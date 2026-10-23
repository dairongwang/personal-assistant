"""Advertising analytics and the privacy/compliance posture for the separate ad-admin dashboard
(apps/ads-admin; docs/ADVERTISING.md §11).

Everything here is computed from ``ad_daily_stats`` — anonymous daily totals per campaign and
coarse region — so the analytics can never be more personal than their source. One extra
safeguard is applied on the way out: **small-count suppression**. A breakdown that says "1 view
from Liechtenstein on Tuesday" could be linked to a person by someone who knows them, so
per-region figures under ``ADS_ANALYTICS_MIN_COUNT`` impressions are folded into an "other"
bucket (statistical disclosure control, the same idea census offices use).

``compliance()`` turns the design promises of docs/ADVERTISING.md §5 into *live* checks against
the running configuration and database, and maps them onto the privacy regimes the product may
meet around the world. It is an engineering aid for the operator — not legal advice.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import ad_sources
import ads
import config
import models
import schemas

OTHER = "other"
IDENTIFYING_WORDS = (
    "account", "device", "user", "ip", "agent", "session", "cookie", "fingerprint", "email", "token",
    "lat", "lon", "city", "postal", "zip",
)  # fmt: skip
AD_TABLES = ("ad_campaigns", "ad_daily_stats")
ALLOWED_COLUMNS = {"created_by"}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _ctr(impressions: int, clicks: int) -> float:
    return round(clicks / impressions, 4) if impressions else 0.0


def _pct_change(now: int, before: int) -> float | None:
    """Relative change vs. the previous window; None when there is no baseline to compare with."""
    if before == 0:
        return None
    return round((now - before) / before, 4)


# ---------------------------------------------------------------------------- analytics


def _rows(db: Session, since: date, until: date, kind: str | None):
    stmt = (
        select(
            models.AdDailyStat.day,
            models.AdDailyStat.region,
            models.AdDailyStat.impressions,
            models.AdDailyStat.clicks,
            models.AdCampaign,
        )
        .join(models.AdCampaign, models.AdCampaign.id == models.AdDailyStat.ad_id)
        .where(models.AdDailyStat.day >= since, models.AdDailyStat.day <= until)
    )
    if kind:
        stmt = stmt.where(models.AdCampaign.kind == kind)
    return db.execute(stmt).all()


def suppress_regions(by_region: dict[str, list[int]], min_count: int) -> tuple[list[schemas.AdStatPoint], int]:
    """Fold regions with fewer than ``min_count`` impressions into one "other" row. Returns
    (rows sorted by impressions, number of regions folded)."""
    kept: list[schemas.AdStatPoint] = []
    other = [0, 0]
    folded = 0
    for region, (imp, clk) in by_region.items():
        if region != OTHER and imp >= min_count:
            kept.append(schemas.AdStatPoint(key=region, impressions=imp, clicks=clk))
        else:
            other[0] += imp
            other[1] += clk
            folded += 1
    kept.sort(key=lambda p: (-p.impressions, p.key))
    if other[0] or other[1]:
        kept.append(schemas.AdStatPoint(key=OTHER, impressions=other[0], clicks=other[1]))
    return kept, folded


def analytics(db: Session, days: int = 30, kind: str | None = None) -> schemas.AdAnalytics:
    today = _today()
    since = today - timedelta(days=days - 1)
    prev_since = since - timedelta(days=days)
    rows = _rows(db, prev_since, today, kind)
    min_count = max(1, config.ADS_ANALYTICS_MIN_COUNT)

    by_day: dict[date, list[int]] = {since + timedelta(days=i): [0, 0] for i in range(days)}
    by_region: dict[str, list[int]] = {}
    by_kind: dict[str, list[int]] = {k: [0, 0] for k in models.AD_KINDS}
    per_campaign: dict[str, list[int]] = {}
    campaign_rows: dict[str, models.AdCampaign] = {}
    cur = [0, 0]
    prev = [0, 0]
    for day, region, imp, clk, campaign in rows:
        if day < since:
            prev[0] += imp
            prev[1] += clk
            continue
        cur[0] += imp
        cur[1] += clk
        by_day[day][0] += imp
        by_day[day][1] += clk
        r = by_region.setdefault(region, [0, 0])
        r[0] += imp
        r[1] += clk
        k = by_kind.setdefault(campaign.kind, [0, 0])
        k[0] += imp
        k[1] += clk
        c = per_campaign.setdefault(campaign.id, [0, 0])
        c[0] += imp
        c[1] += clk
        campaign_rows[campaign.id] = campaign

    # Campaigns with no traffic in the window still belong in the table (they may be live but idle).
    stmt = select(models.AdCampaign).where(models.AdCampaign.status != "archived")
    if kind:
        stmt = stmt.where(models.AdCampaign.kind == kind)
    for campaign in db.execute(stmt).scalars():
        campaign_rows.setdefault(campaign.id, campaign)
        per_campaign.setdefault(campaign.id, [0, 0])

    regions, folded = suppress_regions(by_region, min_count)
    campaigns = [
        schemas.AdCampaignPerformance(
            id=c.id,
            kind=c.kind,
            sponsor=c.sponsor,
            headline=c.headline,
            status=c.status,
            live=ads.is_live(c),
            weight=c.weight,
            regions=c.regions,
            impressions=per_campaign[c.id][0],
            clicks=per_campaign[c.id][1],
            ctr=_ctr(*per_campaign[c.id]),
            share=round(per_campaign[c.id][0] / cur[0], 4) if cur[0] else 0.0,
            impressions_total=c.impressions_total,
            clicks_total=c.clicks_total,
        )
        for c in sorted(
            campaign_rows.values(), key=lambda c: (-per_campaign[c.id][0], -per_campaign[c.id][1], c.sponsor)
        )
    ]
    active_source = ad_sources.describe(ad_sources.active_kind(db))
    return schemas.AdAnalytics(
        days=days,
        since=since.isoformat(),
        until=today.isoformat(),
        kind=kind,
        impressions=cur[0],
        clicks=cur[1],
        ctr=_ctr(*cur),
        previous_impressions=prev[0],
        previous_clicks=prev[1],
        impressions_change=_pct_change(cur[0], prev[0]),
        clicks_change=_pct_change(cur[1], prev[1]),
        by_day=[schemas.AdStatPoint(key=d.isoformat(), impressions=v[0], clicks=v[1]) for d, v in by_day.items()],
        by_kind=[
            schemas.AdKindPerformance(
                kind=k,
                impressions=v[0],
                clicks=v[1],
                ctr=_ctr(*v),
                campaigns=sum(1 for c in campaign_rows.values() if c.kind == k),
            )
            for k, v in by_kind.items()
        ],
        by_region=regions,
        regions_folded=folded,
        min_count=min_count,
        campaigns=campaigns,
        active_source=ad_sources.public(active_source),
        third_party_note=(
            "Third-party sources (Google AdSense, Microsoft Advertising, network tags) count views and clicks in "
            "their own consoles; these figures cover first-party and server-to-server creatives only."
        ),
    )


def export_csv(db: Session, days: int = 30) -> str:
    """Aggregate export (day × campaign × region) with the same small-count suppression applied
    per day, so the file can go to a sponsor or an auditor as is."""
    today = _today()
    since = today - timedelta(days=days - 1)
    min_count = max(1, config.ADS_ANALYTICS_MIN_COUNT)
    buckets: dict[tuple[date, str], dict[str, list[int]]] = {}
    campaigns: dict[str, models.AdCampaign] = {}
    for day, region, imp, clk, campaign in _rows(db, since, today, None):
        campaigns[campaign.id] = campaign
        b = buckets.setdefault((day, campaign.id), {}).setdefault(region, [0, 0])
        b[0] += imp
        b[1] += clk
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["day", "campaign_id", "kind", "sponsor", "headline", "region", "impressions", "clicks", "ctr"])
    for (day, cid), regions in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        c = campaigns[cid]
        rows, _ = suppress_regions(regions, min_count)
        for r in rows:
            w.writerow(
                [
                    day.isoformat(),
                    cid,
                    c.kind,
                    c.sponsor,
                    c.headline,
                    r.key,
                    r.impressions,
                    r.clicks,
                    _ctr(r.impressions, r.clicks),
                ]
            )
    return out.getvalue()


# ---------------------------------------------------------------------------- compliance


def _check(key: str, title: str, ok: bool, detail: str, severity: str = "fail") -> schemas.ComplianceCheck:
    return schemas.ComplianceCheck(key=key, title=title, status="ok" if ok else severity, detail=detail)


def checks(db: Session, app=None) -> list[schemas.ComplianceCheck]:
    out: list[schemas.ComplianceCheck] = []
    meta = models.Base.metadata

    offenders = []
    for table in AD_TABLES:
        for col in meta.tables[table].columns:
            name = col.name.lower()
            if name not in ALLOWED_COLUMNS and any(w in name for w in IDENTIFYING_WORDS):
                offenders.append(f"{table}.{col.name}")
    out.append(
        _check(
            "no_identifiers",
            "Ad tables hold no identifiers",
            not offenders,
            "Columns of ad_campaigns / ad_daily_stats scanned for account, device, user, IP, agent, session, cookie, "
            "fingerprint, e-mail, token, coordinates, city or postal fields: "
            + ("none found." if not offenders else ", ".join(offenders)),
        )
    )
    stats = meta.tables["ad_daily_stats"]
    out.append(
        _check(
            "coarse_granularity",
            "Counters are per day and per country only",
            stats.c.region.type.length == 2 and stats.c.day.type.python_type is date,
            f"region column length {stats.c.region.type.length} (ISO 3166-1 alpha-2), day column type "
            f"{stats.c.day.type.python_type.__name__}; no timestamps per event, no per-event rows.",
        )
    )

    oldest = db.execute(select(func.min(models.AdDailyStat.day))).scalar()
    total_rows = db.execute(select(func.count()).select_from(models.AdDailyStat)).scalar_one()
    age = (_today() - oldest).days if oldest else 0
    out.append(
        _check(
            "retention",
            f"Daily counters deleted after {config.ADS_STATS_RETENTION_DAYS} days",
            age <= config.ADS_STATS_RETENTION_DAYS,
            f"{total_rows} daily rows; oldest is {age} days old" + (f" ({oldest.isoformat()})" if oldest else "") + ".",
        )
    )
    out.append(
        _check(
            "small_count_suppression",
            "Small regional counts are suppressed in reports",
            config.ADS_ANALYTICS_MIN_COUNT >= 2,
            f"Regions with fewer than {config.ADS_ANALYTICS_MIN_COUNT} impressions are folded into “other” "
            "in the dashboard and the CSV export (ADS_ANALYTICS_MIN_COUNT).",
            severity="warn",
        )
    )
    out.append(
        _check(
            "no_ip_geolocation",
            "Country comes from the device, not from the IP address",
            not config.ADS_REGION_HEADER,
            "ADS_REGION_HEADER is empty: the server never derives a location from the IP address."
            if not config.ADS_REGION_HEADER
            else f"ADS_REGION_HEADER={config.ADS_REGION_HEADER}: the edge's IP-derived country is used when a device "
            "sends no region. Disclose this in the privacy policy.",
            severity="warn",
        )
    )
    out.append(
        _check(
            "first_party_images",
            "Creative images only from first-party hosts",
            True,
            "Text-only creatives (no image host allowed)."
            if not config.ADS_IMAGE_HOSTS
            else "Allowed hosts: " + ", ".join(sorted(config.ADS_IMAGE_HOSTS)) + " — sponsors never see viewers' IPs.",
        )
    )
    bad_links = [
        c.id
        for c in db.execute(select(models.AdCampaign).where(models.AdCampaign.status == "active")).scalars()
        if not (c.target_url.lower().startswith("https://") or c.target_url in schemas.APP_LINKS)
    ]
    out.append(
        _check(
            "https_links",
            "Every active creative links to https (or into the app)",
            not bad_links,
            "All active campaigns checked." if not bad_links else f"Non-https links in: {', '.join(bad_links)}",
        )
    )

    source = ad_sources.describe(ad_sources.active_kind(db))
    if source.third_party:
        out.append(
            _check(
                "third_party_consent",
                f"Third-party source ({source.provider_name}) loads only after consent",
                source.third_party,  # consent_required == third_party (ad_sources.public)
                "The panel shows a consent card and loads the provider's tag only after “Allow”; consent is per "
                "provider, device-local and withdrawable. For EEA/UK/CH traffic Google additionally requires a "
                "certified TCF consent-management platform.",
            )
        )
        out.append(
            _check(
                "cmp_required",
                "Certified consent-management platform (EEA / UK / Switzerland)",
                False,
                f"Active source {source.provider_name} is a third party: wire a certified CMP into the consent card "
                "before serving EEA/UK/CH traffic, and list the provider in the privacy policy.",
                severity="warn",
            )
        )
    else:
        out.append(
            _check(
                "third_party_consent",
                "No third-party code runs in the user's browser",
                True,
                f"Active source: {ad_sources.TITLES[source.kind]}"
                + (
                    " (creatives relayed server-to-server; the browser never contacts the network)."
                    if source.kind == "network"
                    else " (first-party)."
                ),
            )
        )
    out.append(
        _check(
            "fakes_off_in_prod",
            "Fake providers disabled in production",
            not (config.IS_PROD and config.ADS_FAKE_PROVIDERS),
            f"APP_ENV={config.ENV}, ADS_FAKE_PROVIDERS={'on' if config.ADS_FAKE_PROVIDERS else 'off'}.",
        )
    )
    if app is not None:
        unsecured = True
        for route in app.routes:
            if getattr(route, "path", "") in ("/api/v1/ads/serve", "/api/v1/ads/metrics"):
                dependant = getattr(route, "dependant", None)
                if dependant is not None and getattr(dependant, "security_requirements", []):
                    unsecured = False
        out.append(
            _check(
                "anonymous_endpoints",
                "Ad requests carry no sign-in",
                unsecured,
                "/ads/serve and /ads/metrics have no authentication; the client never sends its token to them.",
            )
        )
    audits = db.execute(
        select(func.count()).select_from(models.AuditLog).where(models.AuditLog.action.like("ad.%"))
    ).scalar_one()
    out.append(
        _check(
            "audit_trail",
            "Every campaign and source change is audited",
            True,
            f"{audits} ad.* entries in the audit log (who changed what, when).",
        )
    )
    out.append(
        _check(
            "rate_limited",
            "Anonymous endpoints are rate-limited without storing addresses",
            True,
            f"Sliding window of {ads.METRICS_LIMITER.limit} calls / {ads.METRICS_LIMITER.window // 60} min per caller, "
            "keyed by a per-process salted hash kept in memory only.",
        )
    )
    return out


# Regimes the product may meet. `depends_on` names checks whose failure moves the regime to "attention".
REGIMES: tuple[dict, ...] = (
    {
        "code": "GDPR",
        "name": "General Data Protection Regulation + ePrivacy Directive",
        "jurisdiction": "European Union / EEA",
        "obligations": [
            "Lawful basis for any processing (Art. 6); consent for storing/reading information on the device "
            "for ads (ePrivacy Art. 5(3))",
            "Transparency at the point of processing (Art. 13) and an absolute right to object to direct "
            "marketing (Art. 21(2))",
            "Data minimisation and privacy by design (Art. 5, 25)",
        ],
        "how_met": [
            "Nothing personal is stored: counters per ad × day × country; the ad request carries no sign-in",
            "“Why this?” explainer; device-level off switch; region control",
            "Third-party tags only after consent (consent card); server-to-server network mode",
        ],
        "depends_on": ["no_identifiers", "coarse_granularity", "retention", "third_party_consent", "no_ip_geolocation"],
    },
    {
        "code": "UK-GDPR",
        "name": "UK GDPR + PECR",
        "jurisdiction": "United Kingdom",
        "obligations": ["As GDPR; PECR reg. 6 consent for cookies/similar technologies"],
        "how_met": ["Same design; consent card before any third-party tag"],
        "depends_on": ["no_identifiers", "third_party_consent"],
    },
    {
        "code": "nFADP",
        "name": "Federal Act on Data Protection (revised)",
        "jurisdiction": "Switzerland",
        "obligations": ["Proportionality, transparency, privacy by design; information duties for profiling"],
        "how_met": ["No profiling; anonymous aggregates; explainer"],
        "depends_on": ["no_identifiers", "coarse_granularity"],
    },
    {
        "code": "CCPA",
        "name": "CCPA / CPRA and other US state privacy laws (VA, CO, CT, UT, TX, OR, …)",
        "jurisdiction": "United States (state level)",
        "obligations": [
            "Opt-out of “sale”/“sharing” and of cross-context behavioural advertising; honour Global Privacy Control",
            "Notice at collection",
        ],
        "how_met": [
            "No personal information is sold or shared; no cross-context behavioural advertising",
            "GPC/DNT honoured (automatic region detection off); device off switch; explainer as notice",
        ],
        "depends_on": ["no_identifiers", "third_party_consent"],
    },
    {
        "code": "COPPA",
        "name": "Children's Online Privacy Protection Act",
        "jurisdiction": "United States (federal)",
        "obligations": ["No behavioural advertising to children under 13 without verifiable parental consent"],
        "how_met": ["No behavioural targeting for anyone; content policy excludes creatives aimed at minors"],
        "depends_on": ["no_identifiers"],
    },
    {
        "code": "LGPD",
        "name": "Lei Geral de Proteção de Dados",
        "jurisdiction": "Brazil",
        "obligations": ["Legal basis, purpose limitation, transparency; rights of the data subject"],
        "how_met": ["Anonymised data is out of scope (Art. 12); explainer and controls"],
        "depends_on": ["no_identifiers", "retention"],
    },
    {
        "code": "PIPEDA",
        "name": "PIPEDA + Quebec Law 25",
        "jurisdiction": "Canada",
        "obligations": ["Meaningful consent for tracking technologies; privacy by default (Law 25)"],
        "how_met": ["No tracking technology; third-party tags consent-gated; defaults are the most private"],
        "depends_on": ["third_party_consent", "no_identifiers"],
    },
    {
        "code": "APPI",
        "name": "Act on the Protection of Personal Information",
        "jurisdiction": "Japan",
        "obligations": ["Rules for third-party provision and “personally referable information” (cookies/IDs)"],
        "how_met": ["No identifiers collected; third-party providers only with consent"],
        "depends_on": ["no_identifiers", "third_party_consent"],
    },
    {
        "code": "PIPA",
        "name": "Personal Information Protection Act",
        "jurisdiction": "South Korea",
        "obligations": ["Consent for behavioural advertising and for providing data to third parties"],
        "how_met": ["No behavioural advertising; consent card for third parties"],
        "depends_on": ["no_identifiers", "third_party_consent"],
    },
    {
        "code": "PIPL",
        "name": "Personal Information Protection Law",
        "jurisdiction": "China",
        "obligations": [
            "Opt-out of personalised ads; separate consent for third-party provision and cross-border transfer"
        ],
        "how_met": ["Ads are not personalised; third-party tags consent-gated; no personal data leaves the server"],
        "depends_on": ["no_identifiers", "third_party_consent"],
    },
    {
        "code": "DPDP",
        "name": "Digital Personal Data Protection Act",
        "jurisdiction": "India",
        "obligations": ["Consent-based processing; purpose limitation; no tracking/targeted ads at children"],
        "how_met": ["No personal data processed for ads; no targeting of children"],
        "depends_on": ["no_identifiers"],
    },
    {
        "code": "APP",
        "name": "Privacy Act 1988 (Australian Privacy Principles)",
        "jurisdiction": "Australia",
        "obligations": ["Collection only of necessary information; notice; de-identification"],
        "how_met": ["Country + language only, derived on the device; anonymous aggregates"],
        "depends_on": ["no_identifiers", "coarse_granularity"],
    },
    {
        "code": "DSA",
        "name": "Digital Services Act Art. 26 (ad transparency)",
        "jurisdiction": "European Union (online platforms)",
        "obligations": ["Show that it is an ad, who paid, and the main parameters used to pick it"],
        "how_met": ["Badge + sponsor name on every creative; “SHOWN FOR country + language” line; “Why this?”"],
        "depends_on": [],
    },
)


def compliance(db: Session, app=None) -> schemas.AdCompliance:
    results = checks(db, app)
    by_key = {c.key: c for c in results}
    regimes = []
    for r in REGIMES:
        failing = [k for k in r["depends_on"] if k in by_key and by_key[k].status != "ok"]
        regimes.append(
            schemas.ComplianceRegime(
                code=r["code"],
                name=r["name"],
                jurisdiction=r["jurisdiction"],
                obligations=r["obligations"],
                how_met=r["how_met"],
                status="attention" if failing else "ok",
                attention=[by_key[k].title for k in failing],
            )
        )
    total_rows = db.execute(select(func.count()).select_from(models.AdDailyStat)).scalar_one()
    campaigns = db.execute(select(func.count()).select_from(models.AdCampaign)).scalar_one()
    inventory = [
        schemas.DataInventoryItem(
            data="Campaigns (our creatives)",
            location="ad_campaigns",
            personal=False,
            retention="until archived; never deleted (reporting history)",
            rows=campaigns,
        ),
        schemas.DataInventoryItem(
            data="Views/clicks per campaign × UTC day × country (ISO alpha-2)",
            location="ad_daily_stats",
            personal=False,
            retention=f"{config.ADS_STATS_RETENTION_DAYS} days",
            rows=total_rows,
        ),
        schemas.DataInventoryItem(
            data="Active ad source (operator setting)",
            location="app_settings",
            personal=False,
            retention="until changed",
            rows=1,
        ),
        schemas.DataInventoryItem(
            data="Administrator actions on campaigns/sources",
            location="audit_log (ad.*)",
            personal=False,
            retention="as the audit log",
            rows=db.execute(
                select(func.count()).select_from(models.AuditLog).where(models.AuditLog.action.like("ad.%"))
            ).scalar_one(),
        ),
        schemas.DataInventoryItem(
            data="Region + language of an ad request",
            location="HTTPS request only (in memory)",
            personal=False,
            retention="not stored",
            rows=0,
        ),
        schemas.DataInventoryItem(
            data="Rate-limit counters",
            location="process memory (salted hash of the caller address)",
            personal=True,
            retention="10-minute sliding window, never on disk",
            rows=0,
        ),
        schemas.DataInventoryItem(
            data="User's ad preferences (on/off, region choice, hidden creatives, provider consent)",
            location="the user's device (localStorage)",
            personal=True,
            retention="until the user changes them",
            rows=0,
        ),
    ]
    return schemas.AdCompliance(
        generated_at=datetime.now(timezone.utc),
        environment=config.ENV,
        active_source=ad_sources.public(ad_sources.describe(ad_sources.active_kind(db))),
        checks=results,
        regimes=regimes,
        inventory=inventory,
        disclaimer=(
            "Automated engineering checks against the running configuration and database, mapped to the privacy "
            "regimes this product may meet. They are not legal advice: confirm the assessment with counsel for the "
            "markets you serve and keep the privacy policy in step with the active ad source."
        ),
    )
