import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { ApiService } from '../account/api.service';
import { MembershipService } from '../membership/membership.service';
import { type AdRegionMode, SettingsService } from '../settings/settings.service';
import { UiStateService, isTyping } from '../ui/ui-state.service';
import {
  type DetectedRegion,
  NO_REGION,
  browserHints,
  detectRegion,
  isCountryCode,
  primaryLanguage,
} from './region';

/** What the server sends — nothing about the viewer is in here or goes back (see docs/ADVERTISING.md). */
export interface AdCreative {
  id: string;
  sponsor: string;
  headline: string;
  blurb: string;
  cta_label: string;
  target_url: string;
  image_url: string | null;
}

/** Where the panel's content comes from (server: ad_sources.py). */
export type AdSourceKind =
  'internal' | 'sponsored' | 'affiliate' | 'adsense' | 'microsoft' | 'network';

export interface AdEmbed {
  script_url: string;
  script_attrs: Record<string, string>;
  container_tag: string;
  container_class: string;
  container_style: string;
  container_attrs: Record<string, string>;
  push_global: string | null;
}

export interface AdSource {
  kind: AdSourceKind;
  /** Badge text: "Sponsored", "Internal ad", "Affiliate link", "Ad · Google AdSense" … */
  label: string;
  title: string;
  mode: 'fake' | 'live' | 'unavailable';
  /** creatives = first-party cards from /ads/serve · embed = the provider's script tag. */
  render: 'creatives' | 'embed';
  third_party: boolean;
  consent_required: boolean;
  provider_name: string;
  privacy_url: string;
  disclosure: string;
  embed: AdEmbed | null;
}

interface AdServeResponse {
  ads: AdCreative[];
  region: string;
  rotation_seconds: number;
  enabled: boolean;
  source: AdSource;
}

export type ConsentState = 'granted' | 'denied';
const CONSENT_KEY = 'pa.ads.consent.v1';
const REVISIT_REFRESH_MS = 60 * 1000; // back on the tab after this long → ask the server again

export type AdEventKind = 'impression' | 'click';
interface AdEvent {
  ad_id: string;
  kind: AdEventKind;
  region: string;
}

const DISMISSED_KEY = 'pa.ads.dismissed.v1';
export const DISMISS_DAYS = 7;
const ROTATION_TICK_MS = 15_000;
const REFRESH_AFTER_MS = 15 * 60 * 1000; // ask for a fresh batch when the queue wraps after this long
const FLUSH_DELAY_MS = 3_000;
const MAX_BATCH = 20;

/**
 * Sponsored content for the free memberships — first-party, contextual, anonymous.
 *
 *  - Shown only when the membership strategy lacks `ad_free` (Pro/Admin never see it), the
 *    device-level switch is on, the panel is not collapsed, and the device is online.
 *  - The request carries a coarse region and language chosen *on the device* (region.ts; the
 *    user can override or switch it off) and is sent **without** the account token, so a
 *    creative can never be tied to an account.
 *  - "Tracking" is one anonymous impression per creative per batch (once ≥50 % visible for
 *    1 s — the IAB viewability rule, measured by the panel) and one click; they are batched and
 *    posted with `keepalive`, again without a token. The server keeps daily totals per ad and
 *    region, nothing per person.
 *  - Rotation never happens while a dialog is open, the user is typing or the tab is hidden.
 *  - Browsers signalling Global Privacy Control / Do Not Track get no automatic region.
 */
@Injectable({ providedIn: 'root' })
export class AdsService {
  private readonly api = inject(ApiService);
  private readonly membership = inject(MembershipService);
  private readonly settings = inject(SettingsService);
  private readonly ui = inject(UiStateService);

  /** Browser hints (time zone, locale) — a signal so tests can swap them. */
  readonly hints = signal(browserHints());
  readonly online = signal(typeof navigator === 'undefined' ? true : navigator.onLine);
  readonly creatives = signal<AdCreative[]>([]);
  readonly index = signal(0);
  readonly rotationSeconds = signal(300);
  readonly serverEnabled = signal(true);
  /** The active source as the server reported it with the last batch (null until loaded). */
  readonly source = signal<AdSource | null>(null);
  /** Per-provider consent for third-party tags (device-local; nothing is loaded before 'granted'). */
  private readonly consent = signal<Partial<Record<AdSourceKind, ConsentState>>>(loadConsent());
  readonly loading = signal(false);
  readonly lastError = signal<string | null>(null);
  private readonly dismissed = signal<Record<string, number>>(loadDismissed());
  private readonly sessionStats = signal({ impressions: 0, clicks: 0 });

  private loadedAt = 0;
  private loadedKey: string | null = null;
  private shownSince = 0;
  private counted = new Set<string>();
  private queue: AdEvent[] = [];
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private rotationTimer: ReturnType<typeof setInterval> | null = null;
  private readonly started = signal(false);

  /** Privacy signals from the browser (Global Privacy Control, Do Not Track). */
  readonly privacySignal: boolean =
    typeof navigator !== 'undefined' &&
    ((navigator as Navigator & { globalPrivacyControl?: boolean }).globalPrivacyControl === true ||
      navigator.doNotTrack === '1');

  readonly enabled = computed(() => this.settings.settings().adsEnabled);
  readonly collapsed = computed(() => this.settings.settings().adsCollapsed);
  /** Large showcase layout (debug/demo) vs. the compact card. */
  readonly large = computed(() => this.settings.settings().adsLarge);
  readonly regionMode = computed<AdRegionMode>(() => this.settings.settings().adsRegionMode);
  readonly manualRegion = computed(() => this.settings.settings().adsRegion);
  readonly detected = computed<DetectedRegion>(() => detectRegion(this.hints()));
  readonly language = computed(() => primaryLanguage(this.hints().language));

  /** Mode actually applied: a GPC/DNT signal turns automatic detection off (a manual choice is kept). */
  readonly effectiveMode = computed<AdRegionMode>(() => {
    const mode = this.regionMode();
    return mode === 'auto' && this.privacySignal ? 'off' : mode;
  });

  /** Region sent with the request: a country, `ZZ` (user chose none), or null (nothing known). */
  readonly region = computed<string | null>(() => {
    switch (this.effectiveMode()) {
      case 'off':
        return NO_REGION;
      case 'manual': {
        const r = this.manualRegion();
        return isCountryCode(r) ? r : NO_REGION;
      }
      default:
        return this.detected().region;
    }
  });

  /** Whether this device shows sponsored content at all (strip or card). */
  readonly eligible = computed(
    () => this.enabled() && !this.membership.adFree() && this.serverEnabled(),
  );
  /** Creatives not hidden by the user. */
  readonly visible = computed(() => {
    const hidden = this.dismissed();
    const now = Date.now();
    return this.creatives().filter((a) => !(hidden[a.id] && hidden[a.id] > now));
  });
  /** The creative on screen right now, or null (nothing to show / panel folded / offline). */
  readonly current = computed<AdCreative | null>(() => {
    if (!this.eligible() || this.collapsed() || !this.online()) return null;
    const list = this.visible();
    if (!list.length) return null;
    return list[this.index() % list.length] ?? null;
  });
  readonly stats = this.sessionStats.asReadonly();

  /** The source renders through the provider's own script (AdSense, Microsoft, network tag). */
  readonly embedMode = computed(() => this.source()?.render === 'embed');
  /** Consent state for the active third-party source (undefined = not asked yet). */
  readonly consentState = computed<ConsentState | undefined>(() => {
    const src = this.source();
    return src ? this.consent()[src.kind] : undefined;
  });
  /** A third-party tag may only be loaded after the user allowed it. */
  readonly embedAllowed = computed(() => {
    const src = this.source();
    if (!src || src.render !== 'embed') return false;
    return !src.consent_required || this.consentState() === 'granted';
  });
  /** Badge text for the panel. */
  readonly badge = computed(() => this.source()?.label ?? 'Sponsored');

  private readonly contextKey = computed(() => `${this.region() ?? '-'}|${this.language() ?? '-'}`);

  constructor() {
    // (Re)load whenever the panel becomes wanted or its context (region/language) changes.
    effect(() => {
      if (!this.started()) return;
      const wanted = this.eligible() && !this.collapsed() && this.online();
      const key = this.contextKey();
      if (!wanted) return;
      if (this.loadedKey !== key || Date.now() - this.loadedAt > REFRESH_AFTER_MS) {
        untracked(() => void this.load());
      }
    });
  }

  /** Called once by the shell after the account/membership state is known. */
  init(): void {
    if (this.started()) return;
    if (typeof window !== 'undefined') {
      window.addEventListener('online', () => this.online.set(true));
      window.addEventListener('offline', () => this.online.set(false));
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) this.flush();
        // Back on the tab: pick up a source switched by an administrator meanwhile.
        else if (this.loadedKey && Date.now() - this.loadedAt > REVISIT_REFRESH_MS)
          void this.load();
      });
      window.addEventListener('pagehide', () => this.flush());
    }
    this.rotationTimer = setInterval(() => this.tick(), ROTATION_TICK_MS);
    this.started.set(true);
  }

  /** Ask the server for creatives for the current context (no token, ever). */
  async load(): Promise<void> {
    if (this.loading()) return;
    this.loading.set(true);
    const key = this.contextKey();
    try {
      const params = new URLSearchParams();
      const region = this.region();
      if (region) params.set('region', region);
      const lang = this.language();
      if (lang) params.set('lang', lang);
      const qs = params.toString();
      const res = await this.api.get<AdServeResponse>(`/ads/serve${qs ? '?' + qs : ''}`, {
        auth: false,
      });
      this.serverEnabled.set(res.enabled);
      this.source.set(res.source ?? null);
      this.rotationSeconds.set(Math.max(30, res.rotation_seconds || 300));
      this.creatives.set(res.ads);
      this.index.set(0);
      this.counted = new Set();
      this.shownSince = Date.now();
      this.lastError.set(null);
    } catch (e) {
      // Never bother the user about ads: the panel simply stays empty.
      this.lastError.set(e instanceof Error ? e.message : String(e));
      this.creatives.set([]);
    } finally {
      this.loadedAt = Date.now();
      this.loadedKey = key;
      this.loading.set(false);
    }
  }

  /** Rotation: advance when the current creative has been up long enough and it is a calm moment. */
  tick(now = Date.now()): void {
    if (!this.current()) return;
    if (now - this.shownSince < this.rotationSeconds() * 1000) return;
    if (this.ui.dialogsOpen() > 0 || isTyping()) return;
    if (typeof document !== 'undefined' && document.hidden) return;
    this.next(now);
  }

  next(now = Date.now()): void {
    const n = this.visible().length;
    if (n === 0) return;
    const nextIndex = (this.index() + 1) % n;
    if (nextIndex === 0 && now - this.loadedAt > REFRESH_AFTER_MS) {
      void this.load();
      return;
    }
    this.index.set(nextIndex);
    this.shownSince = now;
  }

  /** The panel reports a creative as viewed (≥50 % visible for ≥1 s). Counted once per batch. */
  impression(ad: AdCreative): void {
    if (this.counted.has(ad.id)) return;
    this.counted.add(ad.id);
    this.sessionStats.update((s) => ({ ...s, impressions: s.impressions + 1 }));
    this.enqueue({ ad_id: ad.id, kind: 'impression', region: this.region() ?? NO_REGION });
  }

  /** The user followed the link (the anchor itself navigates; this only counts). */
  click(ad: AdCreative): void {
    this.sessionStats.update((s) => ({ ...s, clicks: s.clicks + 1 }));
    this.enqueue({ ad_id: ad.id, kind: 'click', region: this.region() ?? NO_REGION });
    this.flush();
  }

  /** Hide one creative on this device for a week. */
  dismiss(ad: AdCreative): void {
    const until = Date.now() + DISMISS_DAYS * 24 * 3600 * 1000;
    const next = { ...pruneDismissed(this.dismissed()), [ad.id]: until };
    this.dismissed.set(next);
    saveDismissed(next);
    this.shownSince = Date.now();
  }

  isDismissed(id: string): boolean {
    const until = this.dismissed()[id];
    return !!until && until > Date.now();
  }

  // ---- third-party consent (device-local) ----

  setConsent(kind: AdSourceKind, state: ConsentState): void {
    const next = { ...this.consent(), [kind]: state };
    this.consent.set(next);
    try {
      localStorage.setItem(CONSENT_KEY, JSON.stringify(next));
    } catch {
      /* private mode / quota */
    }
  }

  /** Withdraw = forget the decision; the provider is asked for again next time (and nothing loads until then). */
  withdrawConsent(kind: AdSourceKind): void {
    const next = { ...this.consent() };
    delete next[kind];
    this.consent.set(next);
    try {
      localStorage.setItem(CONSENT_KEY, JSON.stringify(next));
    } catch {
      /* ignore */
    }
  }

  // ---- user controls (device-local settings) ----

  setEnabled(on: boolean): void {
    this.settings.update({ adsEnabled: on });
    if (!on) {
      this.creatives.set([]);
      this.loadedKey = null;
    }
  }

  setLarge(on: boolean): void {
    this.settings.update({ adsLarge: on });
  }

  setCollapsed(on: boolean): void {
    this.settings.update({ adsCollapsed: on });
    if (!on) this.shownSince = Date.now();
  }

  setRegionMode(mode: AdRegionMode, region: string | null = this.manualRegion()): void {
    this.settings.update({
      adsRegionMode: mode,
      adsRegion: mode === 'manual' && isCountryCode(region) ? region : this.manualRegion(),
    });
  }

  // ---- anonymous metrics ----

  private enqueue(ev: AdEvent): void {
    this.queue.push(ev);
    if (this.queue.length >= MAX_BATCH) {
      this.flush();
      return;
    }
    if (!this.flushTimer) this.flushTimer = setTimeout(() => this.flush(), FLUSH_DELAY_MS);
  }

  /** Post whatever is queued. `keepalive` lets the request finish even if the tab closes. */
  flush(): void {
    if (this.flushTimer) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }
    if (!this.queue.length) return;
    const events = this.queue.splice(0, MAX_BATCH);
    try {
      void fetch(this.api.base + '/ads/metrics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ events }),
        keepalive: true,
        credentials: 'omit', // never cookies, never a token
        cache: 'no-store',
      }).catch(() => undefined);
    } catch {
      /* fetch unavailable (very old engine) — counts are best effort */
    }
    if (this.queue.length) this.flushTimer = setTimeout(() => this.flush(), FLUSH_DELAY_MS);
  }

  /** Tests / teardown. */
  stop(): void {
    if (this.rotationTimer) clearInterval(this.rotationTimer);
    if (this.flushTimer) clearTimeout(this.flushTimer);
    this.rotationTimer = this.flushTimer = null;
  }
}

function loadConsent(): Partial<Record<AdSourceKind, ConsentState>> {
  try {
    const raw = JSON.parse(localStorage.getItem(CONSENT_KEY) ?? '{}');
    const out: Partial<Record<AdSourceKind, ConsentState>> = {};
    if (raw && typeof raw === 'object') {
      for (const [k, v] of Object.entries(raw)) {
        if (v === 'granted' || v === 'denied') out[k as AdSourceKind] = v;
      }
    }
    return out;
  } catch {
    return {};
  }
}

function loadDismissed(): Record<string, number> {
  try {
    const raw = JSON.parse(localStorage.getItem(DISMISSED_KEY) ?? '{}');
    return pruneDismissed(typeof raw === 'object' && raw ? raw : {});
  } catch {
    return {};
  }
}

function pruneDismissed(map: Record<string, number>): Record<string, number> {
  const now = Date.now();
  const out: Record<string, number> = {};
  for (const [id, until] of Object.entries(map))
    if (typeof until === 'number' && until > now) out[id] = until;
  return out;
}

function saveDismissed(map: Record<string, number>): void {
  try {
    localStorage.setItem(DISMISSED_KEY, JSON.stringify(map));
  } catch {
    /* private mode / quota */
  }
}
