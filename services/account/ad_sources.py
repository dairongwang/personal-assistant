"""Ad sources — where the panel's content comes from (docs/ADVERTISING.md §10).

Six kinds, one active at a time (``config.ADS_SOURCE``, overridable at runtime by an
administrator; the choice lives in ``app_settings``):

  internal   our own promotions ("house ads", e.g. Go Pro)  → our campaigns, kind=internal
  sponsored  paid messages we host ourselves                 → our campaigns, kind=sponsored
  affiliate  product links that earn us a commission         → our campaigns, kind=affiliate (+ disclosure)
  adsense    Google AdSense                                  → Google's script tag, consent-gated embed
  microsoft  Microsoft Advertising (pubCenter)               → Microsoft's script tag, consent-gated embed
  network    any other ad network                            → JSON fetched server-to-server (default),
                                                                or the network's script tag (embed)

Every third-party provider has a **fake** (``ADS_FAKE_PROVIDERS``, on outside prod): a script
served by this API that renders an unmistakable "FAKE" box through exactly the same embed path
the real tag uses, so the integration can be tested without an account at the provider and
without a single request leaving the machine. The first-party kinds get dummy campaigns from
``seed.py``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.orm import Session

import config
import models
import schemas

log = logging.getLogger("ads")

KINDS = ("internal", "sponsored", "affiliate", "adsense", "microsoft", "network")
FIRST_PARTY_KINDS = ("internal", "sponsored", "affiliate")
SETTING_KEY = "ads.source"
FAKE_PREFIX = "/api/v1/ads/fake"

LABELS = {
    "internal": "Internal ad",
    "sponsored": "Sponsored",
    "affiliate": "Affiliate link",
    "adsense": "Ad · Google AdSense",
    "microsoft": "Ad · Microsoft Advertising",
    "network": "Ad · third-party network",
}
TITLES = {
    "internal": "Internal ads",
    "sponsored": "Sponsored content",
    "affiliate": "Affiliate links",
    "adsense": "Google AdSense",
    "microsoft": "Microsoft Advertising",
    "network": "Third-party network",
}
DISCLOSURES = {
    "internal": "A message from the makers of Personal Assistant — not a paid placement.",
    "sponsored": "Paid placement. The sponsor pays us to show this message; we choose it by country and language only.",
    "affiliate": "Affiliate link: if you buy through it we may earn a commission. The price is the same for you.",
}


@dataclass
class SourceInfo:
    kind: str
    mode: str  # fake | live | unavailable
    reason: str = ""
    embed: dict[str, object] | None = None  # script-tag description for the client (third-party kinds)
    provider_name: str = ""
    privacy_url: str = ""
    reporting: str = ""
    settings: dict[str, str] = field(default_factory=dict)  # what an administrator sees (ids, urls — no secrets)

    @property
    def label(self) -> str:
        if self.kind == "network" and self.provider_name:
            return f"Ad · {self.provider_name}"
        return LABELS[self.kind]

    @property
    def third_party(self) -> bool:
        return self.kind in ("adsense", "microsoft") or (self.kind == "network" and self.embed is not None)

    @property
    def render(self) -> str:
        return "embed" if self.embed is not None else "creatives"


# ---------------------------------------------------------------------------- describing sources


def _fake_url(name: str) -> str:
    return f"{FAKE_PREFIX}/{name}"


def describe(kind: str) -> SourceInfo:
    """What this deployment can do for a kind, given its configuration."""
    fakes = config.ADS_FAKE_PROVIDERS
    if kind in FIRST_PARTY_KINDS:
        info = SourceInfo(kind=kind, mode="live", reporting="Admin panel → Sponsored content (anonymous totals)")
        if kind == "affiliate":
            info.settings = {"affiliate_tag": config.ADS_AFFILIATE_TAG or "(none — set ADS_AFFILIATE_TAG)"}
        return info

    if kind == "adsense":
        live = bool(config.ADS_ADSENSE_CLIENT and config.ADS_ADSENSE_SLOT)
        if not live and not fakes:
            return SourceInfo(
                kind, "unavailable", "Set ADS_ADSENSE_CLIENT and ADS_ADSENSE_SLOT (or ADS_FAKE_PROVIDERS=true)."
            )
        client = config.ADS_ADSENSE_CLIENT if live else "ca-pub-0000000000000000"
        slot = config.ADS_ADSENSE_SLOT if live else "0000000000"
        script = config.ADS_ADSENSE_SCRIPT_URL if live else _fake_url("adsense.js")
        return SourceInfo(
            kind,
            "live" if live else "fake",
            embed={
                "script_url": f"{script}?client={client}",
                "script_attrs": {"crossorigin": "anonymous"},
                "container_tag": "ins",
                "container_class": "adsbygoogle",
                "container_style": "display:block;text-decoration:none",
                "container_attrs": {
                    "data-ad-client": client,
                    "data-ad-slot": slot,
                    "data-ad-format": "auto",
                    "data-full-width-responsive": "true",
                },
                "push_global": "adsbygoogle",
            },
            provider_name="Google AdSense",
            privacy_url="https://policies.google.com/technologies/ads",
            reporting="Google AdSense console (adsense.google.com)",
            settings={"client": client, "slot": slot, "script_url": script},
        )

    if kind == "microsoft":
        live = bool(
            config.ADS_MICROSOFT_SITE_ID and config.ADS_MICROSOFT_PUBLISHER_ID and config.ADS_MICROSOFT_AD_UNIT_ID
        )
        if not live and not fakes:
            return SourceInfo(
                kind,
                "unavailable",
                "Set ADS_MICROSOFT_SITE_ID, ADS_MICROSOFT_PUBLISHER_ID and ADS_MICROSOFT_AD_UNIT_ID "
                "(or ADS_FAKE_PROVIDERS=true).",
            )
        site = config.ADS_MICROSOFT_SITE_ID if live else "000000"
        publisher = config.ADS_MICROSOFT_PUBLISHER_ID if live else "000000000"
        unit = config.ADS_MICROSOFT_AD_UNIT_ID if live else "000000000"
        script = config.ADS_MICROSOFT_SCRIPT_URL if live else _fake_url("microsoft.js")
        return SourceInfo(
            kind,
            "live" if live else "fake",
            embed={
                "script_url": f"{script}?siteId={site}&publisherId={publisher}",
                "script_attrs": {},
                "container_tag": "div",
                "container_class": "pubcenter-ad",
                "container_style": "display:block",
                "container_attrs": {"data-ad-unit-id": unit, "data-ad-size": "responsive"},
                "push_global": None,
            },
            provider_name="Microsoft Advertising",
            privacy_url="https://privacy.microsoft.com/privacystatement",
            reporting="Microsoft pubCenter (pubcenter.microsoft.com)",
            settings={"site_id": site, "publisher_id": publisher, "ad_unit_id": unit, "script_url": script},
        )

    # network
    name = config.ADS_NETWORK_NAME
    if config.ADS_NETWORK_JSON_URL:
        return SourceInfo(
            kind,
            "live",
            provider_name=name or "ad network",
            privacy_url=config.ADS_NETWORK_PRIVACY_URL,
            reporting=f"{name or 'The network'}'s own console",
            settings={"mode": "json (server-to-server)", "json_url": config.ADS_NETWORK_JSON_URL, "name": name},
        )
    if config.ADS_NETWORK_SCRIPT_URL:
        return SourceInfo(
            kind,
            "live",
            embed=_network_embed(
                config.ADS_NETWORK_SCRIPT_URL, name or "network", config.ADS_NETWORK_UNIT_ID or "default"
            ),
            provider_name=name or "ad network",
            privacy_url=config.ADS_NETWORK_PRIVACY_URL,
            reporting=f"{name or 'The network'}'s own console",
            settings={"mode": "script (client-side)", "script_url": config.ADS_NETWORK_SCRIPT_URL, "name": name},
        )
    if not fakes:
        return SourceInfo(
            kind,
            "unavailable",
            "Set ADS_NETWORK_NAME plus ADS_NETWORK_JSON_URL or ADS_NETWORK_SCRIPT_URL (or ADS_FAKE_PROVIDERS=true).",
        )
    # Fake network: the JSON (server-to-server) flavour, answered by this very API.
    return SourceInfo(
        kind,
        "fake",
        provider_name="Example Ad Network",
        privacy_url="https://example.com/privacy",
        reporting="Example Ad Network console (fake)",
        settings={
            "mode": "json (server-to-server)",
            "json_url": _fake_url("network.json"),
            "name": "Example Ad Network",
        },
    )


def _network_embed(script_url: str, name: str, unit: str) -> dict[str, object]:
    return {
        "script_url": script_url,
        "script_attrs": {},
        "container_tag": "div",
        "container_class": "ad-network-slot",
        "container_style": "display:block",
        "container_attrs": {"data-ad-network": name, "data-ad-unit": unit},
        "push_global": None,
    }


def describe_all() -> list[SourceInfo]:
    return [describe(k) for k in KINDS]


# ---------------------------------------------------------------------------- active source


def active_kind(db: Session) -> str:
    row = db.get(models.AppSetting, SETTING_KEY)
    kind = (row.value if row else config.ADS_SOURCE).strip().lower()
    return kind if kind in KINDS else "sponsored"


def set_active(db: Session, kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"Unknown ad source '{kind}'. One of: {', '.join(KINDS)}.")
    row = db.get(models.AppSetting, SETTING_KEY)
    if row is None:
        row = models.AppSetting(key=SETTING_KEY, value=kind)
        db.add(row)
    else:
        row.value = kind
    db.flush()
    return kind


def public(info: SourceInfo) -> schemas.AdSource:
    return schemas.AdSource(
        kind=info.kind,
        label=info.label,
        title=TITLES[info.kind],
        mode=info.mode,
        render=info.render,
        third_party=info.third_party,
        consent_required=info.third_party,
        provider_name=info.provider_name,
        privacy_url=info.privacy_url,
        disclosure=DISCLOSURES.get(info.kind, ""),
        embed=info.embed,
    )


def admin_out(info: SourceInfo, active: str) -> schemas.AdSourceAdmin:
    return schemas.AdSourceAdmin(
        **public(info).model_dump(),
        active=info.kind == active,
        reason=info.reason,
        reporting=info.reporting,
        settings=info.settings,
    )


# ---------------------------------------------------------------------------- affiliate links


def affiliate_url(url: str) -> str:
    """Append the affiliate tag (``ADS_AFFILIATE_TAG`` = ``key=value``) unless the link already has it."""
    tag = config.ADS_AFFILIATE_TAG
    if not tag or "=" not in tag:
        return url
    key, value = tag.split("=", 1)
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if key in query:
        return url
    query[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


# ---------------------------------------------------------------------------- third-party network (JSON)


class NetworkClient:
    """Server-to-server call to a third-party network's JSON endpoint. Cached for a minute per
    (region, language); failures are logged and yield no ads (never an error for the user).

    Expected answer (normalised here): ``{"ads": [{"id", "sponsor"|"advertiser", "headline"|"title",
    "blurb"|"text", "cta_label"|"cta", "target_url"|"url", "image_url"}]}``."""

    TTL = 60.0

    def __init__(self):
        self._cache: dict[tuple[str, str], tuple[float, list[schemas.AdCreative]]] = {}
        self._lock = threading.Lock()

    def creatives(self, region: str | None, language: str | None) -> list[schemas.AdCreative]:
        key = (region or "ZZ", language or "")
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        if config.ADS_NETWORK_JSON_URL:
            ads = self._fetch(config.ADS_NETWORK_JSON_URL, key[0], key[1])
        else:
            ads = normalize_network_payload(fake_network_payload(key[0], key[1]))
        with self._lock:
            self._cache[key] = (now + self.TTL, ads)
        return ads

    def _fetch(self, template: str, region: str, language: str) -> list[schemas.AdCreative]:
        url = template.replace("{region}", region).replace("{lang}", language)
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": "personal-assistant-ads/1"}
            )
            with urllib.request.urlopen(req, timeout=3) as res:  # noqa: S310 — operator-configured URL
                payload = json.loads(res.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — any network problem = no ads this minute
            log.warning("ad network %s unreachable: %s", url, exc)
            return []
        return normalize_network_payload(payload)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


NETWORK = NetworkClient()


def normalize_network_payload(payload: object) -> list[schemas.AdCreative]:
    items = payload.get("ads", []) if isinstance(payload, dict) else payload
    out: list[schemas.AdCreative] = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("target_url") or raw.get("url") or "")
        if not url.lower().startswith("https://"):
            continue
        image = raw.get("image_url")
        out.append(
            schemas.AdCreative(
                id=f"network:{raw.get('id', len(out))}",
                sponsor=str(raw.get("sponsor") or raw.get("advertiser") or "advertiser")[:80],
                headline=str(raw.get("headline") or raw.get("title") or "")[:90],
                blurb=str(raw.get("blurb") or raw.get("text") or "")[:200],
                cta_label=str(raw.get("cta_label") or raw.get("cta") or "Learn more")[:30],
                target_url=url[:2048],
                image_url=str(image)[:2048]
                if isinstance(image, str) and image.lower().startswith("https://")
                else None,
            )
        )
        if len(out) >= 10:
            break
    return out


def fake_network_payload(region: str, language: str) -> dict:
    """What the fake network answers (also served at /ads/fake/network.json for curl)."""
    place = region if region and region != "ZZ" else "everywhere"
    return {
        "network": "Example Ad Network (FAKE)",
        "request": {"region": region, "lang": language, "note": "answered by this API — no real network was contacted"},
        "ads": [
            {
                "id": f"fake-net-1-{region}",
                "advertiser": "Example Ad Network (FAKE)",
                "title": f"FAKE NETWORK AD — served server-to-server for {place}",
                "text": "This creative came from the fake third-party network endpoint. Your browser never "
                "contacted the network; this server fetched it and relayed it without your IP.",
                "cta": "Open network link",
                "url": "https://example.com/network-ad-1?utm_source=personal-assistant",
            },
            {
                "id": "fake-net-2",
                "advertiser": "Example Ad Network (FAKE)",
                "title": f"FAKE NETWORK AD — second creative ({language or 'any language'})",
                "text": "Networks usually return several creatives; the panel rotates through them.",
                "cta": "Open network link",
                "url": "https://example.com/network-ad-2?utm_source=personal-assistant",
            },
        ],
    }


# ---------------------------------------------------------------------------- fake provider scripts

_FAKE_TEMPLATE = Path(__file__).with_name("static") / "fake-ad-provider.js"
_FAKE_SPECS = {
    "adsense": ("Google AdSense", "#1a73e8", "ins.adsbygoogle", ["data-ad-client", "data-ad-slot", "data-ad-format"]),
    "microsoft": ("Microsoft Advertising", "#107c10", ".pubcenter-ad", ["data-ad-unit-id", "data-ad-size"]),
    "network": ("Third-party network", "#7c3aed", ".ad-network-slot", ["data-ad-network", "data-ad-unit"]),
}


def fake_script(name: str) -> str | None:
    """The fake tag for a provider: static/fake-ad-provider.js with the provider's specifics filled in."""
    spec = _FAKE_SPECS.get(name)
    if not spec:
        return None
    title, color, selector, attrs = spec
    values = {
        "__NAME__": json.dumps(title),
        "__COLOR__": json.dumps(color),
        "__SELECTOR__": json.dumps(selector),
        "__ATTRS__": json.dumps(attrs),
        "__KIND__": json.dumps(name),
        "__MARKER__": json.dumps(f"/ads/fake/{name}.js"),
    }
    script = _FAKE_TEMPLATE.read_text(encoding="utf-8")
    for key, value in values.items():
        script = script.replace(key, value)
    return script
