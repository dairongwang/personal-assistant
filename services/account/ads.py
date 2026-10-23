"""Sponsored content — a first-party, privacy-preserving ad server (docs/ADVERTISING.md).

What it does
  * ``serve()``: pick creatives for a *coarse region* (ISO 3166-1 alpha-2) and language. Both
    come from the client, which derives them on the device (time zone / locale) and lets the
    user see, change or switch them off. Paid memberships never call this.
  * ``record_events()``: add anonymous impression/click counts to ``ad_daily_stats``
    (ad × day × region). That is the whole "tracking": no identifier of any kind is stored.

What it deliberately does not do
  * No behavioural targeting, no profiles, no cookies, no third-party calls, no per-event rows.
  * No IP geolocation unless the operator names an edge header (``ADS_REGION_HEADER``) — and
    even then only when the client sent no region, and the country is never stored.
"""

from __future__ import annotations

import hashlib
import random
import re
import secrets
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

import ad_sources
import config
import models
import schemas

UNKNOWN_REGION = "ZZ"  # ISO 3166-1 user-assigned code: "unknown / no region" (never matches a targeted campaign)
EVENT_KINDS = ("impression", "click")
MAX_EVENTS_PER_REQUEST = 20

_ALPHA2 = re.compile(r"^[A-Z]{2}$")
_LANG = re.compile(r"^[a-z]{2,3}$")

# Region groups a campaign may target instead of listing countries. Expanded at match time.
REGION_GROUPS: dict[str, tuple[str, ...]] = {
    "EU": (
        "AT",
        "BE",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "DE",
        "GR",
        "HU",
        "IE",
        "IT",
        "LV",
        "LT",
        "LU",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SK",
        "SI",
        "ES",
        "SE",
    ),  # fmt: skip
    "DACH": ("DE", "AT", "CH"),
    "BENELUX": ("BE", "NL", "LU"),
    "NORDIC": ("DK", "FI", "IS", "NO", "SE"),
    "UKIE": ("GB", "IE"),
    "NA": ("US", "CA", "MX"),
    "LATAM": (
        "AR",
        "BO",
        "BR",
        "CL",
        "CO",
        "CR",
        "CU",
        "DO",
        "EC",
        "SV",
        "GT",
        "HN",
        "MX",
        "NI",
        "PA",
        "PY",
        "PE",
        "PR",
        "UY",
        "VE",
    ),  # fmt: skip
    "APAC": (
        "AU",
        "BD",
        "BN",
        "KH",
        "CN",
        "HK",
        "IN",
        "ID",
        "JP",
        "KR",
        "LA",
        "MO",
        "MY",
        "MM",
        "NP",
        "NZ",
        "PK",
        "PH",
        "SG",
        "LK",
        "TW",
        "TH",
        "VN",
    ),  # fmt: skip
    "MEA": (
        "AE",
        "BH",
        "DZ",
        "EG",
        "GH",
        "IL",
        "IQ",
        "JO",
        "KE",
        "KW",
        "LB",
        "MA",
        "NG",
        "OM",
        "QA",
        "SA",
        "TN",
        "TR",
        "TZ",
        "UG",
        "ZA",
    ),  # fmt: skip
}
REGION_GROUPS["EEA"] = (*REGION_GROUPS["EU"], "IS", "LI", "NO")


# ---------------------------------------------------------------------------- abuse limiter


class RateLimiter:
    """Sliding-window limiter for the anonymous endpoints, so nobody can inflate the counters
    from a loop. Keys are a per-process salted hash of the caller's address kept **in memory
    only** for the window (PLATFORM_DESIGN.md: rate-limit counters are the one place an IP is
    touched at all, and never for longer than the window)."""

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._salt = secrets.token_bytes(16)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def key(self, address: str | None) -> str:
        return hashlib.sha256(self._salt + (address or "?").encode()).hexdigest()[:32]

    def allow(self, address: str | None, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        k = self.key(address)
        with self._lock:
            q = self._hits.setdefault(k, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 10_000:  # housekeeping: forget idle callers
                for stale in [key for key, hits in self._hits.items() if not hits or hits[-1] <= now - self.window]:
                    del self._hits[stale]
            return True


METRICS_LIMITER = RateLimiter(limit=120, window_seconds=600)  # a real user sends a handful per hour
SERVE_LIMITER = RateLimiter(limit=120, window_seconds=600)


# ---------------------------------------------------------------------------- normalisation


def normalize_region(value: str | None) -> str | None:
    """Client/edge input → ``"DE"`` | ``"ZZ"`` (explicitly none) | ``None`` (nothing sent)."""
    if value is None:
        return None
    code = value.strip().upper()
    if not code:
        return None
    return code if _ALPHA2.match(code) else UNKNOWN_REGION


def normalize_language(value: str | None) -> str | None:
    """``"de-DE"`` / ``"de"`` → ``"de"``; anything odd → None."""
    if not value:
        return None
    primary = value.strip().split(",")[0].split(";")[0].split("-")[0].split("_")[0].lower()
    return primary if _LANG.match(primary) else None


def parse_regions(text: str) -> list[str]:
    """``"de, at ,EU"`` → ``["DE", "AT", "EU"]`` — validated country codes or group names."""
    out: list[str] = []
    for raw in re.split(r"[,\s]+", text or ""):
        token = raw.strip().upper()
        if not token:
            continue
        if token in REGION_GROUPS or _ALPHA2.match(token):
            if token not in out:
                out.append(token)
        else:
            raise ValueError(f"'{raw}' is not a country code (e.g. DE) or region group ({', '.join(REGION_GROUPS)}).")
    return out


def parse_languages(text: str) -> list[str]:
    out: list[str] = []
    for raw in re.split(r"[,\s]+", text or ""):
        lang = normalize_language(raw)
        if raw.strip() and not lang:
            raise ValueError(f"'{raw}' is not a language code (e.g. en, de).")
        if lang and lang not in out:
            out.append(lang)
    return out


def expand_regions(tokens: list[str]) -> set[str]:
    countries: set[str] = set()
    for token in tokens:
        countries.update(REGION_GROUPS.get(token, (token,)))
    return countries


def _csv(tokens: list[str]) -> str:
    return ",".join(tokens)


def _split(text: str) -> list[str]:
    return [t for t in (text or "").split(",") if t]


# ---------------------------------------------------------------------------- eligibility


def is_live(ad: models.AdCampaign, now: datetime | None = None) -> bool:
    """Active, inside its schedule and under its impression budget."""
    now = now or datetime.now(timezone.utc)
    if ad.status != "active":
        return False
    if ad.starts_at and now < ad.starts_at:
        return False
    if ad.ends_at and now >= ad.ends_at:
        return False
    if ad.max_impressions is not None and ad.impressions_total >= ad.max_impressions:
        return False
    return True


def matches(ad: models.AdCampaign, region: str | None, language: str | None) -> bool:
    """Contextual match: worldwide campaigns match everyone; targeted ones need a known region."""
    wanted = _split(ad.regions)
    if wanted:
        if not region or region == UNKNOWN_REGION or region not in expand_regions(wanted):
            return False
    langs = _split(ad.languages)
    if langs and (not language or language not in langs):
        return False
    return True


def _weighted_order(ads: list[models.AdCampaign], rng: random.Random) -> list[models.AdCampaign]:
    """Weighted shuffle: heavier campaigns come first more often, every live campaign still rotates."""
    pool = list(ads)
    out: list[models.AdCampaign] = []
    while pool:
        total = sum(max(1, a.weight) for a in pool)
        pick = rng.uniform(0, total)
        for i, a in enumerate(pool):
            pick -= max(1, a.weight)
            if pick <= 0:
                out.append(pool.pop(i))
                break
        else:  # floating-point edge
            out.append(pool.pop())
    return out


def creative(ad: models.AdCampaign) -> schemas.AdCreative:
    return schemas.AdCreative(
        id=ad.id,
        sponsor=ad.sponsor,
        headline=ad.headline,
        blurb=ad.blurb,
        cta_label=ad.cta_label,
        target_url=ad.target_url,
        image_url=ad.image_url,
    )


def first_party_creatives(
    db: Session, kind: str, region: str | None, language: str | None, limit: int, rng: random.Random
) -> list[schemas.AdCreative]:
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(models.AdCampaign).where(models.AdCampaign.status == "active", models.AdCampaign.kind == kind)
    ).scalars()
    live = [a for a in rows if is_live(a, now) and matches(a, region, language)]
    out = [creative(a) for a in _weighted_order(live, rng)[:limit]]
    if kind == "affiliate":
        for c in out:
            c.target_url = ad_sources.affiliate_url(c.target_url)
    return out


def serve(
    db: Session,
    region: str | None,
    language: str | None,
    limit: int | None = None,
    rng: random.Random | None = None,
) -> schemas.AdServeResponse:
    """What the panel shows for this context: the active source and, for first-party kinds and
    server-to-server networks, the creatives. ``region``/``language`` are already normalised."""
    limit = limit or config.ADS_MAX_PER_RESPONSE
    source = ad_sources.describe(ad_sources.active_kind(db))
    ads: list[schemas.AdCreative] = []
    enabled = config.ADS_ENABLED and source.mode != "unavailable"
    if enabled:
        if source.kind in ad_sources.FIRST_PARTY_KINDS:
            ads = first_party_creatives(db, source.kind, region, language, limit, rng or random.Random())
        elif source.kind == "network" and source.render == "creatives":
            ads = ad_sources.NETWORK.creatives(region, language)[:limit]
    return schemas.AdServeResponse(
        ads=ads,
        region=region or UNKNOWN_REGION,
        rotation_seconds=config.ADS_ROTATION_SECONDS,
        enabled=enabled,
        source=ad_sources.public(source),
    )


# ---------------------------------------------------------------------------- anonymous counting


def _upsert_daily(db: Session, ad_id: str, day: date, region: str, impressions: int, clicks: int) -> None:
    """Atomic ``+=`` on the (ad, day, region) row — dialect-native upsert so concurrent
    requests never lose counts."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    table = models.AdDailyStat.__table__
    stmt = insert(table).values(ad_id=ad_id, day=day, region=region, impressions=impressions, clicks=clicks)
    stmt = stmt.on_conflict_do_update(
        index_elements=["ad_id", "day", "region"],
        set_={
            "impressions": table.c.impressions + stmt.excluded.impressions,
            "clicks": table.c.clicks + stmt.excluded.clicks,
        },
    )
    db.execute(stmt)


def record_events(db: Session, events: list[schemas.AdEventIn], today: date | None = None) -> int:
    """Add the batch to today's counters. Unknown/archived ads and malformed regions are
    dropped silently (the client is not told which — there is nothing to fix on its side).
    One (ad, kind) per request counts once: a retry cannot inflate the numbers."""
    today = today or datetime.now(timezone.utc).date()
    seen: set[tuple[str, str]] = set()
    wanted: dict[tuple[str, str], list[int]] = {}  # (ad_id, region) → [impressions, clicks]
    for ev in events[:MAX_EVENTS_PER_REQUEST]:
        if ev.kind not in EVENT_KINDS or (ev.ad_id, ev.kind) in seen:
            continue
        seen.add((ev.ad_id, ev.kind))
        region = normalize_region(ev.region) or UNKNOWN_REGION
        bucket = wanted.setdefault((ev.ad_id, region), [0, 0])
        bucket[0 if ev.kind == "impression" else 1] += 1
    if not wanted:
        return 0
    ids = {ad_id for ad_id, _ in wanted}
    ads = {
        a.id: a
        for a in db.execute(
            select(models.AdCampaign).where(models.AdCampaign.id.in_(ids), models.AdCampaign.status != "archived")
        ).scalars()
    }
    accepted = 0
    for (ad_id, region), (impressions, clicks) in wanted.items():
        ad = ads.get(ad_id)
        if not ad:
            continue
        _upsert_daily(db, ad_id, today, region, impressions, clicks)
        ad.impressions_total += impressions
        ad.clicks_total += clicks
        accepted += impressions + clicks
    db.commit()
    return accepted


def prune_stats(db: Session, today: date | None = None) -> int:
    """Delete daily rows older than the retention window (the per-campaign totals stay)."""
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=config.ADS_STATS_RETENTION_DAYS)
    result = db.execute(delete(models.AdDailyStat).where(models.AdDailyStat.day < cutoff))
    db.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------------------- administration


def check_image_host(url: str | None) -> None:
    """Creative images must come from a first-party host (config.ADS_IMAGE_HOSTS)."""
    if not url:
        return
    host = (urlsplit(url).hostname or "").lower()
    if host not in config.ADS_IMAGE_HOSTS:
        allowed = ", ".join(sorted(config.ADS_IMAGE_HOSTS)) or "none configured (ADS_IMAGE_HOSTS)"
        raise ValueError(
            f"Images must be hosted on a first-party host so sponsors never see viewers' IP addresses. "
            f"'{host}' is not allowed; allowed: {allowed}."
        )


def apply_input(ad: models.AdCampaign, body: schemas.AdCampaignIn | schemas.AdCampaignPatch) -> dict:
    """Copy validated fields onto the row; returns ``{field: (before, after)}`` for the audit log."""
    changes: dict[str, tuple] = {}
    data = body.model_dump(exclude_unset=True)
    kind = data.get("kind", ad.kind or "sponsored")
    url = data.get("target_url", ad.target_url)
    if url and url.startswith("app:") and kind != "internal":
        raise ValueError("Only internal (house) ads may link into the app (app:upgrade); use an https:// link.")
    if "regions" in data:
        data["regions"] = _csv(parse_regions(data["regions"] or ""))
    if "languages" in data:
        data["languages"] = _csv(parse_languages(data["languages"] or ""))
    if "image_url" in data:
        check_image_host(data["image_url"])
    for field, value in data.items():
        before = getattr(ad, field)
        if before != value:
            changes[field] = (before, value)
            setattr(ad, field, value)
    return changes


def admin_out(ad: models.AdCampaign) -> schemas.AdCampaignOut:
    return schemas.AdCampaignOut(
        id=ad.id,
        kind=ad.kind,
        sponsor=ad.sponsor,
        headline=ad.headline,
        blurb=ad.blurb,
        cta_label=ad.cta_label,
        target_url=ad.target_url,
        image_url=ad.image_url,
        regions=ad.regions,
        languages=ad.languages,
        status=ad.status,
        weight=ad.weight,
        starts_at=ad.starts_at,
        ends_at=ad.ends_at,
        max_impressions=ad.max_impressions,
        impressions_total=ad.impressions_total,
        clicks_total=ad.clicks_total,
        live=is_live(ad),
        created_at=ad.created_at,
        updated_at=ad.updated_at,
    )


def stats(db: Session, ad: models.AdCampaign, days: int) -> schemas.AdStats:
    since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    rows = db.execute(
        select(models.AdDailyStat)
        .where(models.AdDailyStat.ad_id == ad.id, models.AdDailyStat.day >= since)
        .order_by(models.AdDailyStat.day)
    ).scalars()
    by_day: dict[date, list[int]] = {}
    by_region: dict[str, list[int]] = {}
    for r in rows:
        d = by_day.setdefault(r.day, [0, 0])
        d[0] += r.impressions
        d[1] += r.clicks
        g = by_region.setdefault(r.region, [0, 0])
        g[0] += r.impressions
        g[1] += r.clicks
    impressions = sum(v[0] for v in by_day.values())
    clicks = sum(v[1] for v in by_day.values())
    return schemas.AdStats(
        ad_id=ad.id,
        days=days,
        impressions=impressions,
        clicks=clicks,
        ctr=round(clicks / impressions, 4) if impressions else 0.0,
        by_day=[schemas.AdStatPoint(key=d.isoformat(), impressions=v[0], clicks=v[1]) for d, v in by_day.items()],
        by_region=sorted(
            (schemas.AdStatPoint(key=k, impressions=v[0], clicks=v[1]) for k, v in by_region.items()),
            key=lambda p: (-p.impressions, p.key),
        ),
    )


def overview(db: Session) -> schemas.AdOverview:
    today = datetime.now(timezone.utc).date()
    week = db.execute(
        select(
            func.coalesce(func.sum(models.AdDailyStat.impressions), 0),
            func.coalesce(func.sum(models.AdDailyStat.clicks), 0),
        ).where(models.AdDailyStat.day >= today - timedelta(days=6))
    ).one()
    counts = dict(db.execute(select(models.AdCampaign.status, func.count()).group_by(models.AdCampaign.status)).all())
    live = sum(
        1
        for a in db.execute(select(models.AdCampaign).where(models.AdCampaign.status == "active")).scalars()
        if is_live(a)
    )
    return schemas.AdOverview(
        source=ad_sources.public(ad_sources.describe(ad_sources.active_kind(db))),
        enabled=config.ADS_ENABLED,
        campaigns={s: int(counts.get(s, 0)) for s in models.AD_STATUSES},
        live=live,
        impressions_7d=int(week[0]),
        clicks_7d=int(week[1]),
        rotation_seconds=config.ADS_ROTATION_SECONDS,
        retention_days=config.ADS_STATS_RETENTION_DAYS,
        image_hosts=sorted(config.ADS_IMAGE_HOSTS),
        region_groups={k: list(v) for k, v in REGION_GROUPS.items()},
    )
