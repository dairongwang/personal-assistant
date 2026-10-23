# Sponsored content (advertisement management)

| | |
|---|---|
| Status | Implemented — `services/account/ads.py` + `ad_sources.py` + `ad_analytics.py`, `apps/web/src/app/core/ads/` (the panel), **managed from the separate ads admin console** `apps/ads-admin` (§11) |
| Applies to | **anonymous** and **free** memberships. **paid** and **admin** have the `ad_free` feature and never see or request it. |
| Hard rule | Same as the rest of the platform (`PLATFORM_DESIGN.md` §0): the server learns nothing about a person. Ads are **first-party, contextual and anonymous** — there is no ad network, no profile, no cookie, no third-party request, and nothing per user is stored. |

This document is the design, the data inventory and the privacy assessment in one place. It is
written so that the privacy policy and a DPIA can be derived from it.

---

## 1. What the user sees

```
┌ Sidebar ─────────────────────────────────────────────────────┐
│ Today's events                                                │
│ Overdue                                                       │
│ ┌ SPONSORED · Example Coffee Roasters ──── Why this? · Hide ┐ │
│ │ SHOWN FOR [Germany (DE)] + [German (de)]                   │ │
│ │           from this device's time zone                     │ │
│ │ Fresh-roasted coffee for the week ahead                  ✕ │ │
│ │ Roasted in Hamburg, free shipping across …                 │ │
│ │ Visit the shop ↗                                           │ │
│ │ Campaign 1 of 3 for this context · this session: 1 view …  │ │
│ └────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
 v1.0.0 · your data stays on this device · sponsored content
```

Two layouts, switched under *Why this?* → **Large panel** (`adsLarge`, device-local, **on by
default** for now — the showcase/debug view requested for the dashboard):

| | Large (showcase) | Compact (production card) |
|---|---|---|
| Size | ≈ one third of the page: the sidebar column (⅓ of the width on desktop) × `min-height: 33dvh`, yellow border | small dashed card |
| Targeting line `SHOWN FOR country + language` | yes, large highlights | yes, small highlights |
| Session footer (*Campaign n of m · views · clicks*) | yes | no |
| Empty state ("No campaign matches … right now", offline, service down) | shown | panel simply absent |

The targeting line is the feature's transparency in one glance: the **country** (`Germany (DE)`,
`no region (ZZ)`, `unknown region`) and **language** (`English (en)`, `any language`) the request
used, plus where they came from (*from this device's time zone* / *language*, *country chosen by
you*, *region switched off*). See the README section *Seeing the advertisement panel* for the
step-by-step on http://localhost:4200/.

* **One** card, **last** in the sidebar (on phones: at the very end of the page), part of the
  normal page flow — never fixed, never an overlay, never a modal, no animation, no sound, no
  auto-play, no video, one text-first creative at a time. It is also last in DOM order, so
  screen readers reach it after the content (`<aside aria-labelledby="ad-label">`).
* **Clearly labelled**: the `SPONSORED` badge and the sponsor's name are always visible, the link
  has `rel="sponsored nofollow noopener noreferrer"` and an accessible name that starts with
  "Sponsored:". Creatives never imitate app UI (text-first, muted colours, dashed border).
* **Controls on the card**: *Why this?* (the explainer, §4), *Hide* (fold to a one-line strip —
  nothing is requested while folded), **✕** (hide this creative on this device for 7 days).
  The footer link *sponsored content* opens the explainer too, also when the panel is off.
* **Quiet rotation**: a creative stays ≥ `ADS_ROTATION_SECONDS` (default 5 min); the next one is
  shown only when no dialog is open, the user is not typing and the tab is visible. Offline → no
  panel. A failing ad server → no panel, no error message.
* **Pro removes it**: `ad_free` is part of `PAID_FEATURES` on both sides (strategy pattern,
  `docs/MEMBERSHIP.md`); the explainer and the account screen say so.

## 2. How a creative is chosen — "location"

Targeting is **contextual**, by *country* and *language* only. Both are decided **on the device**:

| Input | How | Leaves the device? |
|---|---|---|
| Country | `Intl.DateTimeFormat().resolvedOptions().timeZone` → country via the IANA `zone.tab` table shipped in the app (`zone-country.ts`, generated); fallback: region subtag of `navigator.language` | Only the resulting **two-letter code** (`DE`), never the zone or locale |
| Language | primary subtag of `navigator.language` (`de-CH` → `de`) | Only the two/three-letter code |

* No Geolocation API (no permission prompt, no coordinates), no IP geolocation, no Wi-Fi/cell
  data. `Permissions-Policy: geolocation=()` stays in `staticwebapp.config.json`.
* The user sees the detected country in the explainer and can switch to **a country they
  choose** or to **no region** (worldwide creatives only). The choice is stored on the device
  (`localStorage`, `pa.settings.v1`), never on the server.
* A browser sending **Global Privacy Control** or **Do Not Track** gets no automatic detection
  (mode falls back to *no region*; an explicit manual choice is respected).
* Server side (`ads.py`): a campaign lists countries and/or groups (`EU`, `EEA`, `DACH`,
  `BENELUX`, `NORDIC`, `UKIE`, `NA`, `LATAM`, `APAC`, `MEA`; empty = everywhere) and languages
  (empty = any). A device that reports no region only sees "everywhere" campaigns. Eligible
  campaigns are ordered by a weighted shuffle (`weight` 1–100) and the first
  `ADS_MAX_PER_RESPONSE` are returned; the client rotates through them.
* Optional, **off by default**: `ADS_REGION_HEADER=CF-IPCountry` (or whatever the edge proxy
  sets) lets the server use the CDN's country *only when the client sent no region at all* —
  never when the user chose *no region* (`ZZ`) — and it is never stored. Turning it on is a
  conscious operator decision that belongs in the privacy policy.

## 3. Impression and click tracking — what is counted, and what is not

```
client                                   server (ads.py)                        database
 creative ≥50 % visible for ≥1 s  ──┐
 (IntersectionObserver, IAB rule)    │ POST /api/v1/ads/metrics                ad_daily_stats
 link clicked (new tab)            ──┤ {events:[{ad_id, kind, region}]}  ──▶  (ad_id, day, region)
 batched ≤20, fetch keepalive,       │ no auth, no cookies                     impressions += n
 credentials: 'omit', no token     ──┘                                         clicks += n
```

* **Stored**: per campaign, per UTC day, per country: two integers. Plus two running totals on
  the campaign row (for the budget cap). That is all. There is no event table, no timestamp per
  event, no user/device/session id, no IP address, no user agent, no referrer.
* **Not stored, by construction**: `tests/test_privacy.py::test_ad_tables_cannot_identify_anyone`
  fails the build if either ad table ever gets a column named like an identifier (`account`,
  `device`, `user`, `ip`, `agent`, `session`, `cookie`, `fingerprint`, `email`, `token`, `lat`,
  `lon`, `city`, `postal`, `zip`), if `region` grows beyond two characters, or if `day` becomes a
  timestamp. `test_ad_endpoints_take_and_return_no_identifiers` walks the OpenAPI schema of the
  public endpoints for the same words and asserts they carry no `security` requirement.
* **Deduplication**: one impression per creative per served batch on the client; one
  (ad, kind) per request on the server, so a retry cannot inflate numbers. Unknown/archived ads
  are dropped silently. Counts are added with a dialect-native upsert (no lost updates).
* **Click** = the user followed the link. The anchor navigates natively (`target="_blank"`,
  `referrerpolicy="no-referrer"`); the count is sent alongside, never through a redirect, so
  there is no per-click server log line with a destination. The sponsor learns nothing about the
  viewer (no referrer; attribution via their own UTM parameters).
* **Abuse**: both anonymous endpoints are rate-limited per caller (sliding window, 120 calls /
  10 min) with an in-memory, per-process **salted hash** of the address that is forgotten when
  the window passes — the one place the platform touches an IP at all, and never on disk.
* **Retention**: daily rows older than `ADS_STATS_RETENTION_DAYS` (default 400 — thirteen months
  of year-over-year comparison) are deleted at API start and whenever an administrator opens the
  overview. Campaign totals stay.
* **Reporting** (administrators): views, clicks, CTR, per day and per country for the last *n*
  days — `GET /api/v1/admin/ads/{id}/stats?days=30`.

## 4. Transparency and control ("Why this?")

The explainer dialog (`ad-info-dialog.ts`) shows, in plain language and with the live values of
this device:

| Section | Content |
|---|---|
| Used to pick it | Region: *Germany (detected on this device)* / *chosen by you* / *none* · Language · Membership |
| Never used | tasks, events, notes; account or e-mail; precise location; browsing history; cookies; third-party trackers — "the request is sent without your sign-in" |
| What is counted | views and clicks per message, per day, per country — anonymous totals |
| Shared with sponsors | nothing; links open in a new tab without a referrer |
| Controls | **Show sponsored content on this device** (on/off) · **Pick offers for**: detected / a country I choose / no region · **Go Pro — no sponsored content** |
| Signals | a note when GPC/DNT is active |

Everything the user decides here is device-local. Turning sponsored content **off** removes the
panel and stops every ad request on that device until it is turned on again (footer link).

## 5. Privacy-law assessment

The design goal was to stay out of the *personalised advertising* regime altogether, because
the product promise is "no data about you on our servers". Summary of the analysis per regime —
to be confirmed by counsel before launch, and reflected in the privacy policy:

| Regime | Question | Answer in this design |
|---|---|---|
| **GDPR** — personal data? | Is anything stored about an identified/identifiable person? | No. Daily counters per country are anonymous statistics (Recital 26). The only personal data touched is the IP address in transit, as for any HTTPS request; it is not logged by the application (existing policy: rate-limit counters ≤ 24 h, no access-log retention beyond the hoster's default). |
| GDPR Art. 6 — legal basis for *processing in the moment* (region + language sent with the request) | Legitimate interest (Art. 6(1)(f)): funding a free tier with minimal, non-identifying context, which the user can see and switch off. A short LIA is in §5.1. No consent banner is needed because nothing is stored on the terminal for tracking and no profile is built; a consent dialog would be *more* intrusive than the feature. |
| GDPR Art. 21(2) — right to object to direct marketing | Absolute and honoured **in-app**: the *Show sponsored content* switch; plus per-creative hide and the region switch. |
| GDPR Art. 13/14 — information | The explainer (§4) is the notice at the point of processing; the privacy policy section is derived from §2–§3. |
| GDPR Art. 22 / profiling | None — no profile, no inference, no behavioural signal. |
| GDPR Art. 25/32 — by design / security | Tables constrained by tests; https-only creative links; images only from first-party hosts (`ADS_IMAGE_HOSTS`, default: text only) so sponsors never receive viewers' IPs; links open with no referrer; admin actions audited. |
| **ePrivacy (Art. 5(3)) / cookie rules** | Is information stored on or read from the terminal for advertising? | No cookies and no identifiers. `localStorage` holds only the user's own choices (on/off, region, folded, hidden creatives) — strictly necessary to honour what the user asked for; no consent required. The time zone/locale are read to render the app anyway and are not transmitted. |
| **CCPA/CPRA** (California) | "Sale" or "sharing" of personal information? Cross-context behavioural advertising? | Neither: no personal information is disclosed to anyone, and no cross-context data is used. No *Do Not Sell or Share* link is legally required; the off switch is offered anyway. **GPC is honoured** (automatic region detection is disabled when the signal is present). |
| **Children** (COPPA / GDPR Art. 8 / DSA Art. 28) | Profiling minors? | No profiling at all. Creatives are reviewed by an administrator; no sensitive categories (§6). |
| **DSA Art. 26** (online platforms) | Ad transparency | Each ad shows that it is an ad, who the sponsor is, and the main parameters used to pick it (country/language) — exactly what the explainer provides, even though a local-first task app is unlikely to be in scope. |
| **Other regimes** (UK GDPR/PECR, LGPD, PIPEDA, APPI …) | Same principles; nothing personal is stored, processing is transparent and objectionable. |

### 5.1 Legitimate-interest assessment (short form)

*Purpose:* fund the free tier. *Necessity:* a worldwide-only panel would work too; coarse country
and language markedly improve relevance (local shipping, language of the offer) at negligible
privacy cost. *Balancing:* data is minimal (two short codes), device-derived, not stored, not
linked to the account (the request deliberately carries no token), visible to the user, and the
user can opt out with one switch or choose a region by hand. Reasonable expectations: users of
a free product expect some sponsored content; they do not expect tracking — the design gives
them the former without the latter. *Outcome:* legitimate interest prevails; safeguards as
listed in §2–§4.

### 5.2 Data inventory (for the records of processing)

| Data | Where | Who | Retention |
|---|---|---|---|
| Region (2 letters), language (≤3 letters) | HTTPS request parameters | Account Service, in memory for the request | not stored |
| Ad preferences (on/off, region mode, chosen country, folded, hidden creatives) | `localStorage` on the device | user | until the user changes them / clears site data |
| Impressions & clicks per ad × day × country | `ad_daily_stats` | administrators | `ADS_STATS_RETENTION_DAYS` (400) |
| Campaign totals | `ad_campaigns.impressions_total/clicks_total` | administrators | life of the campaign |
| Campaigns (our creatives) | `ad_campaigns` | administrators | archived, never deleted (history) |
| Admin actions (`ad.create/update/archive`) | `audit_log` | administrators | as the rest of the audit log |

## 6. Content policy (operator side)

Administrators are the review step. Creatives must be factual and clearly commercial; no
deception, no imitation of app controls or system messages, no sensitive categories (health,
political, religious, sexual orientation, financial distress, alcohol/tobacco/gambling, anything
aimed at minors), no affiliate cloaking, https links only, images only on first-party hosts.
Sponsors receive aggregate reports, never raw data.

## 7. Administration

The **separate ads admin console** (`apps/ads-admin`, §11) or the API directly (`role = admin`,
everything audited). The user app contains no advertising management; its admin dialog links to
the console (`ADS_ADMIN_URL`):

| Action | API |
|---|---|
| Overview (serving on/off, live count, last-7-day totals, rotation, retention, image hosts, region groups) | `GET /api/v1/admin/ads/overview` |
| List / create / read / update / archive campaigns | `GET /admin/ads?include_archived=` · `POST /admin/ads` · `GET /admin/ads/{id}` · `PATCH /admin/ads/{id}` · `DELETE /admin/ads/{id}` (archive) |
| Per-campaign report | `GET /admin/ads/{id}/stats?days=30` → totals, CTR, by day, by country |

Campaign fields: `sponsor`, `headline` (≤ 90), `blurb` (≤ 200), `cta_label` (≤ 30), `target_url`
(https), `image_url` (https, allow-listed host, optional), `regions`, `languages`, `status`
(`draft` · `active` · `paused` · `archived`), `weight` (1–100), `starts_at`, `ends_at`,
`max_impressions` (budget cap). A campaign is *live* when it is active, inside its schedule and
under its cap — the list shows `live`, `scheduled`, `ended`, `budget spent`, `paused`, `draft`.

Public endpoints (no authentication; the client never sends its token):

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/ads/serve?region=DE&lang=de` | Creatives for this context (`region` omitted = unknown, `ZZ` = user chose none) |
| `POST /api/v1/ads/metrics` | `{events: [{ad_id, kind: impression\|click, region?}]}`, ≤ 20 per call |

## 8. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ADS_ENABLED` | `true` | Kill switch: `false` serves nothing (the client hides the panel) |
| `ADS_ROTATION_SECONDS` | `300` | Minimum time a creative stays on screen |
| `ADS_MAX_PER_RESPONSE` | `3` | Creatives per `/ads/serve` answer |
| `ADS_STATS_RETENTION_DAYS` | `400` | Daily counters older than this are deleted |
| `ADS_IMAGE_HOSTS` | *(empty = text only)* | Comma-separated first-party hosts creative images may be loaded from |
| `ADS_REGION_HEADER` | *(empty = off)* | Edge header with the visitor's country, used only when the client sent no region; never stored |

Development: `python dev.py run` (with `APP_ENV` ≠ `prod`) seeds eleven campaigns
(`seed.py::FAKE_ADS`): five **dummy advertisements** that say "DUMMY AD" in the headline —
worldwide, English-only, EEA, APAC, NA + LATAM — and six fictional ones with finer targeting
(DACH in German and in English, EU, US/CA, a paused one). `python dev.py seed --ads` (re)creates
only the campaigns (`--force` when `APP_ENV=prod`, where nothing is seeded automatically). Change
the browser's time zone/language or pick a country in *Why this?* to see the targeting change;
sign in as `pro@example.com` to see the panel disappear, as `admin@example.com` to manage
campaigns.

## 9. Tests

| Where | What |
|---|---|
| `services/account/tests/test_ads.py` | region/language/group matching, status/schedule/budget/limit, weighted rotation, edge-header opt-in semantics, anonymous aggregation (dedupe, unknown ads, pruning), admin CRUD + audit, validation (https, image hosts, regions, languages, schedule), `ad_free` by strategy, seeded campaigns |
| `services/account/tests/test_privacy.py` | ad tables cannot identify anyone; public ad endpoints carry no identifiers and no auth |
| `apps/web/src/app/core/ads/region.spec.ts` | time zone / locale → country, aliases, no guessing from UTC, country picker |
| `apps/web/src/app/core/ads/ads.service.spec.ts` | no token ever (anonymous and signed-in free), `ad_free` hides and stops requests, device switch, folded panel, region modes (`ZZ`, manual, auto), impression dedupe + keepalive/credentials-omit metrics, dismiss persistence, calm-moment rotation, kill switch/offline, silent failure |
| `e2e/test_ads.py` | real browser: German device sees a labelled DACH/EU creative requested without a token, impression after 1 s, explainer, hide/fold/off switch, Pro removes it; US device + manual region; administrator creates a campaign, an Irish visitor views and clicks it, the report shows `1 views · 1 clicks · IE` and nothing else; pause/archive |

## 10. Ad sources (configurable)

The panel is a *slot*; what fills it is the **ad source** — one of six, configured by
`ADS_SOURCE` and switchable at runtime by an administrator (ads admin console → *Ad source*;
persisted in `app_settings`, audited as `ad.source`). `GET /api/v1/ads/serve`
returns the active source next to the creatives, and the client renders accordingly
(`ad_sources.py`, `ads.service.ts`, `ad-panel.ts`, `ad-embed.ts`).

| Kind | Content | Delivery | Label on the panel | Privacy posture |
|---|---|---|---|---|
| `internal` — Internal ads | Our own promotions ("house ads"), campaigns with `kind = internal`; may link into the app (`app:upgrade` opens the account screen) | first-party | **Internal ad** + *"…not a paid placement"* | Same as sponsored: contextual, anonymous totals. |
| `sponsored` — Sponsored content | Paid placements we host ourselves (`kind = sponsored`) — §1–§5 | first-party | **Sponsored** + *"Paid placement…"* | §5 applies unchanged. |
| `affiliate` — Affiliate links | Product links earning a commission (`kind = affiliate`); `ADS_AFFILIATE_TAG` is appended at serve time | first-party | **Affiliate link** + the prominent FTC/ASA-style disclosure *"…we may earn a commission. The price is the same for you."* | Same as sponsored; the merchant only sees the click (no referrer), never the viewer's context. |
| `adsense` — Google AdSense | Google's tag (`adsbygoogle.js` + `<ins class="adsbygoogle">`) | **embed**, client-side | **Ad · Google AdSense** | **Third party**: Google sets cookies and processes IP/device data → the panel shows a **consent card** first; nothing is loaded before *Allow* (GDPR Art. 6(1)(a) / ePrivacy Art. 5(3)); consent is per provider, device-local, withdrawable under *Why this?*. GPC/DNT: we still ask — the user decides. A consent-management platform (TCF) is required by Google for EEA/UK traffic in production; wire it into `ad-embed.ts` where the consent card is. |
| `microsoft` — Microsoft Advertising | pubCenter tag (`sdk.js?siteId&publisherId` + `<div class="pubcenter-ad" data-ad-unit-id>`) | **embed**, client-side | **Ad · Microsoft Advertising** | As AdSense (third party, consent-gated). |
| `network` — Third-party network | Any network. **JSON mode** (default, `ADS_NETWORK_JSON_URL`): *this server* calls the network (region/language only, no viewer data), normalises the answer and serves it like first-party creatives. **Script mode** (`ADS_NETWORK_SCRIPT_URL`): the network's tag, consent-gated like AdSense | server-to-server *or* embed | **Ad · \<network name\>** | JSON mode: the visitor's browser never contacts the network (no IP, no cookies) — the privacy-preserving way to use a network. Script mode: third party, consent-gated. |

**Fakes.** `ADS_FAKE_PROVIDERS` (default: on outside `prod`) makes every third-party provider
available without credentials: the embed path loads `/api/v1/ads/fake/<provider>.js` — a
script served by this API (`static/fake-ad-provider.js`) that renders an unmistakable dashed
"<Provider> — FAKE" box into the very container the real tag would use (for AdSense it also
implements `adsbygoogle.push({})`), and the network source answers from
`/api/v1/ads/fake/network.json`. The panel shows a red **FAKE** chip. In `prod` fakes are off and
an unconfigured provider is **unavailable** (the panel stays empty rather than showing a fake).
The first-party kinds have dummy campaigns from `seed.py` (internal, sponsored, affiliate).

**Configuration.**

| Variable | Default | Meaning |
|---|---|---|
| `ADS_SOURCE` | `sponsored` | Default source (the admin switch overrides it) |
| `ADS_FAKE_PROVIDERS` | `true` outside prod | Built-in fakes for providers without credentials |
| `ADS_AFFILIATE_TAG` | – | `key=value` appended to affiliate links |
| `ADS_ADSENSE_CLIENT`, `ADS_ADSENSE_SLOT` (`ADS_ADSENSE_SCRIPT_URL`) | – | AdSense publisher id and ad unit |
| `ADS_MICROSOFT_SITE_ID`, `ADS_MICROSOFT_PUBLISHER_ID`, `ADS_MICROSOFT_AD_UNIT_ID` (`ADS_MICROSOFT_SCRIPT_URL`) | – | Microsoft pubCenter tag values (check them against the tag pubCenter gives you) |
| `ADS_NETWORK_NAME`, `ADS_NETWORK_JSON_URL` *or* `ADS_NETWORK_SCRIPT_URL`, `ADS_NETWORK_UNIT_ID`, `ADS_NETWORK_PRIVACY_URL` | – | Third-party network; `{region}`/`{lang}` are substituted into the JSON URL |

**Reporting.** First-party kinds: anonymous totals in the admin panel (§3). Third-party kinds:
the provider's own console (their tag counts; we do not double-count). Network JSON mode:
the network's console.

**Tests.** `tests/test_ad_sources.py` (catalogue, switching + persistence + audit, what each
source serves, unavailable vs. live with credentials, fake scripts/endpoint, JSON normalisation
and failure tolerance, affiliate tagging, kind rules), `ads.service.spec.ts` (consent gating),
`e2e/test_ad_sources.py` (administrator switches through all six; the visitor sees each one —
consent card before any provider request, fake tag rendered through the provider's markup,
withdraw, server-to-server network with no outbound browser request, affiliate tag, `app:` link).
The README section *Testing each advertisement source* is the manual checklist.

## 11. The ads admin console and its analytics

Advertising is operated from a **separate dashboard** — `apps/ads-admin` — rather than from
inside the user app, so it can be developed, released and hosted on its own (different audience,
different cadence, different security perimeter). It is a dependency-free static single-page app
(ES modules + SVG charts) that authenticates like the PWA (browser-side PBKDF2/HKDF, the password
never leaves the browser; `role = admin` required) and uses the API's `/admin/ads/*` endpoints.
Hosting: its own nginx image (`apps/ads-admin/Dockerfile`, `ADS_ADMIN_API_BASE`, CSP with
`connect-src` limited to the API, `X-Frame-Options: DENY`, `noindex`) and `CORS_ORIGINS` on the
API; or, for single-process setups, `ADS_ADMIN_DIST` mounts it at `/ads-admin/` (outside the
PWA's service-worker scope). **The console is the only advertising-management UI**: the user
app's admin dialog merely links to it (`ADS_ADMIN_URL` → `AdminStats.ads_admin_url`), and the
browser test `e2e/test_ads_admin.py::test_console_is_separate_from_the_user_app` enforces the
separation (no management UI or `/admin/ads/*` calls from the user app; the console works on its
own origin with only its own files plus the API). The same console also hosts the
administrators' **payment analytics** (*Payments* section, `/admin/payments/*` —
docs/DEPLOYMENT.md "Payments"); advertising and billing data never mix: the ad tables stay
anonymous, the payment views are account-level by design and administrator-only.

### 11.1 Analytics (`GET /admin/ads/analytics`, `…/analytics.csv`)

| Figure | Definition |
|---|---|
| Impressions / clicks / CTR | Sums over `ad_daily_stats` in the window; CTR = clicks ÷ impressions |
| Change | vs. the window of the same length immediately before (`null` when that window is empty) |
| By day | One point per calendar day (UTC), zero-filled |
| By source kind | internal / sponsored / affiliate (a campaign's `kind`), with the number of campaigns |
| By country | Per ISO alpha-2 region — **small-count suppression** (below) |
| Campaigns | Every non-archived campaign (idle ones with zeros): status, live, weight, share of impressions, all-time totals |
| CSV | day × campaign × country with the same suppression per day — fit to hand to a sponsor or an auditor |

Third-party sources (AdSense, Microsoft, network script tags) measure themselves in their own
consoles; the dashboard says so instead of double-counting.

**Small-count suppression.** Aggregates are only as anonymous as their smallest cell. "1 view
from Liechtenstein on Tuesday" is a cell someone who knows the only Liechtenstein user could
attribute. Therefore regional figures under `ADS_ANALYTICS_MIN_COUNT` impressions (default 5)
are folded into an `other` row in the overview and in the CSV, and the response states how many
regions were folded (`regions_folded`). Per-campaign stats (`/admin/ads/{id}/stats`) remain
raw daily totals per country for operational debugging and are labelled as such.

### 11.2 Privacy & compliance (`GET /admin/ads/compliance`)

Turns the promises of §5 into **live checks** against the running system:

| Check | Verifies |
|---|---|
| `no_identifiers` | no column in the ad tables named like an identifier (same word list as `test_privacy.py`) |
| `coarse_granularity` | `region` is 2 characters, `day` is a date |
| `retention` | oldest daily row ≤ `ADS_STATS_RETENTION_DAYS` |
| `small_count_suppression` | `ADS_ANALYTICS_MIN_COUNT ≥ 2` |
| `no_ip_geolocation` | `ADS_REGION_HEADER` empty (warn otherwise — disclose it) |
| `first_party_images` | image allow-list (text-only when empty) |
| `https_links` | every active creative links to https or `app:` |
| `third_party_consent` / `cmp_required` | a third-party source is consent-gated; CMP reminder for EEA/UK/CH |
| `fakes_off_in_prod` | no fake providers in `APP_ENV=prod` |
| `anonymous_endpoints` | `/ads/serve` and `/ads/metrics` carry no security requirement (inspected on the live app) |
| `audit_trail`, `rate_limited` | audit entries exist; limiter configuration |

The same response carries the **data inventory** (what the feature stores, where, personal or
not, retention, live row counts) and the **regime map**: GDPR + ePrivacy, UK GDPR + PECR, Swiss
nFADP, CCPA/CPRA and other US state laws, COPPA, LGPD, PIPEDA + Quebec Law 25, APPI, PIPA, PIPL,
DPDP, Australian APPs, DSA Art. 26 — each with its key obligations for advertising, how this
design meets them, and the checks it depends on; a failing check moves the regime to
*attention*. The disclaimer is part of the payload: these are engineering checks, not legal
advice.

### 11.3 Tests

`services/account/tests/test_ad_analytics.py` (aggregation, trend, suppression, CSV, compliance
checks reacting to configuration and data drift, API-side mount + CORS), `apps/ads-admin/tests`
(`node --test`: chart geometry, formatting), `e2e/test_ads_admin.py` (real browser: sign-in
rules, KPIs/chart/breakdowns with a folded small country, filters, CSV download, campaign
create/pause/archive, source switch visible to the user API, compliance page, audit log,
sign-out).
