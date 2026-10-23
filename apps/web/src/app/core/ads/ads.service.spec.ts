import { TestBed } from '@angular/core/testing';
import { AccountService, type Me } from '../account/account.service';
import { ApiService } from '../account/api.service';
import { SettingsService } from '../settings/settings.service';
import { UiStateService } from '../ui/ui-state.service';
import { type AdCreative, type AdSource, AdsService, DISMISS_DAYS } from './ads.service';

const SPONSORED: AdSource = {
  kind: 'sponsored',
  label: 'Sponsored',
  title: 'Sponsored content',
  mode: 'live',
  render: 'creatives',
  third_party: false,
  consent_required: false,
  provider_name: '',
  privacy_url: '',
  disclosure: 'Paid placement.',
  embed: null,
};
const ADSENSE: AdSource = {
  kind: 'adsense',
  label: 'Ad · Google AdSense',
  title: 'Google AdSense',
  mode: 'fake',
  render: 'embed',
  third_party: true,
  consent_required: true,
  provider_name: 'Google AdSense',
  privacy_url: 'https://policies.google.com/technologies/ads',
  disclosure: '',
  embed: {
    script_url: '/api/v1/ads/fake/adsense.js?client=ca-pub-0',
    script_attrs: {},
    container_tag: 'ins',
    container_class: 'adsbygoogle',
    container_style: 'display:block',
    container_attrs: { 'data-ad-client': 'ca-pub-0', 'data-ad-slot': '0' },
    push_global: 'adsbygoogle',
  },
};

const CREATIVES: AdCreative[] = [
  {
    id: 'ad-1',
    sponsor: 'Example Co',
    headline: 'First',
    blurb: 'One',
    cta_label: 'Go',
    target_url: 'https://example.com/1',
    image_url: null,
  },
  {
    id: 'ad-2',
    sponsor: 'Example Co',
    headline: 'Second',
    blurb: 'Two',
    cta_label: 'Go',
    target_url: 'https://example.com/2',
    image_url: null,
  },
];

type Call = { url: string; init: RequestInit };

function me(membership: 'free' | 'paid', features: string[]): Me {
  return {
    account_id: 'acc-1',
    email: 'ann@example.com',
    role: 'user',
    created_at: '',
    vault_enabled: true,
    devices: [],
    entitlements: {
      plan: membership === 'paid' ? 'pro' : 'free',
      status: 'active',
      features,
      current_period_end: null,
      cancel_at_period_end: false,
      trial_available: false,
      provider: 'seed',
      access_until: null,
      renews: false,
      can_cancel: false,
      can_manage: false,
      last_payment: null,
      membership,
      membership_label: membership,
      max_active_tasks: membership === 'paid' ? null : 3,
    },
  };
}

describe('AdsService (first-party, contextual, anonymous)', () => {
  let ads: AdsService;
  let account: AccountService;
  let api: ApiService;
  let settings: SettingsService;
  let ui: UiStateService;
  let calls: Call[];
  let serveResponse: {
    ads: AdCreative[];
    region: string;
    rotation_seconds: number;
    enabled: boolean;
    source: AdSource;
  };

  beforeEach(() => {
    localStorage.clear();
    calls = [];
    serveResponse = {
      ads: CREATIVES,
      region: 'DE',
      rotation_seconds: 300,
      enabled: true,
      source: SPONSORED,
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        const body = url.includes('/ads/serve') ? serveResponse : { accepted: 1 };
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }),
    );
    TestBed.configureTestingModule({});
    account = TestBed.inject(AccountService);
    api = TestBed.inject(ApiService);
    settings = TestBed.inject(SettingsService);
    ui = TestBed.inject(UiStateService);
    ads = TestBed.inject(AdsService);
    ads.hints.set({ timeZone: 'Europe/Berlin', language: 'de-DE' });
  });
  afterEach(() => {
    ads.stop();
    vi.unstubAllGlobals();
  });

  const serveCalls = () => calls.filter((c) => c.url.includes('/ads/serve'));
  const metricCalls = () => calls.filter((c) => c.url.includes('/ads/metrics'));
  /** Run pending effects and let the fetch → json → signal chain complete. */
  const settle = async () => {
    for (let i = 0; i < 4; i++) {
      TestBed.tick();
      await new Promise((r) => setTimeout(r, 0));
    }
    TestBed.tick();
  };

  it('anonymous: asks for creatives with the on-device region and language, never with a token', async () => {
    ads.init();
    await settle();
    expect(ads.detected()).toEqual({ region: 'DE', source: 'timezone' });
    expect(serveCalls()).toHaveLength(1);
    expect(serveCalls()[0].url).toMatch(/\/api\/v1\/ads\/serve\?region=DE&lang=de$/);
    expect(
      (serveCalls()[0].init.headers as Record<string, string>)['Authorization'],
    ).toBeUndefined();
    expect(ads.current()?.id).toBe('ad-1');
    expect(ads.eligible()).toBe(true);
  });

  it('signed-in free account: the token is still never sent with ad requests', async () => {
    api.setToken('secret-token');
    account.session.set({
      accountId: 'acc-1',
      deviceId: 'd',
      email: 'ann@example.com',
      vaultEnabled: true,
    });
    account.dataKey.set({} as CryptoKey);
    account.me.set(me('free', []));
    ads.init();
    await settle();
    expect(serveCalls()).toHaveLength(1);
    const headers = serveCalls()[0].init.headers as Record<string, string>;
    expect(headers['Authorization']).toBeUndefined();
    expect(JSON.stringify(serveCalls()[0].init)).not.toContain('secret-token');
  });

  it('paid membership (ad_free): nothing is shown and nothing is requested', async () => {
    account.session.set({
      accountId: 'acc-1',
      deviceId: 'd',
      email: 'ann@example.com',
      vaultEnabled: true,
    });
    account.dataKey.set({} as CryptoKey);
    account.me.set(me('paid', ['sync', 'ad_free']));
    ads.init();
    await settle();
    expect(ads.eligible()).toBe(false);
    expect(ads.current()).toBeNull();
    expect(serveCalls()).toHaveLength(0);
    // Downgrade → the panel comes back.
    account.me.set(me('free', []));
    await settle();
    expect(serveCalls()).toHaveLength(1);
  });

  it('device switch off: no panel, no request; on again reloads', async () => {
    settings.update({ adsEnabled: false });
    ads.init();
    await settle();
    expect(ads.eligible()).toBe(false);
    expect(serveCalls()).toHaveLength(0);
    ads.setEnabled(true);
    await settle();
    expect(serveCalls()).toHaveLength(1);
    expect(ads.current()?.id).toBe('ad-1');
    ads.setEnabled(false);
    await settle();
    expect(ads.current()).toBeNull();
    expect(ads.creatives()).toEqual([]);
  });

  it('collapsed panel: nothing requested until it is unfolded', async () => {
    settings.update({ adsCollapsed: true });
    ads.init();
    await settle();
    expect(ads.eligible()).toBe(true);
    expect(ads.current()).toBeNull();
    expect(serveCalls()).toHaveLength(0);
    ads.setCollapsed(false);
    await settle();
    expect(serveCalls()).toHaveLength(1);
    expect(ads.current()?.id).toBe('ad-1');
  });

  it('region choice: off sends ZZ, manual sends the chosen country, and a change reloads', async () => {
    ads.init();
    await settle();
    ads.setRegionMode('off');
    await settle();
    expect(serveCalls()[1].url).toContain('region=ZZ');
    ads.setRegionMode('manual', 'FR');
    await settle();
    expect(serveCalls()[2].url).toContain('region=FR');
    expect(ads.manualRegion()).toBe('FR');
    ads.setRegionMode('manual', 'nonsense');
    await settle();
    expect(ads.region()).toBe('FR'); // an invalid code does not replace the previous choice
    ads.setRegionMode('auto');
    await settle();
    expect(serveCalls().at(-1)?.url).toContain('region=DE');
  });

  it('no derivable region: the parameter is omitted (worldwide creatives only)', async () => {
    ads.hints.set({ timeZone: 'UTC', language: 'en' });
    ads.init();
    await settle();
    expect(serveCalls()[0].url).toMatch(/\/ads\/serve\?lang=en$/);
    expect(ads.region()).toBeNull();
  });

  it('counts one impression per creative and posts anonymous metrics with keepalive, no credentials', async () => {
    ads.init();
    await settle();
    vi.useFakeTimers();
    try {
      const ad = ads.current()!;
      ads.impression(ad);
      ads.impression(ad); // same creative again: not counted twice
      expect(ads.stats().impressions).toBe(1);
      expect(metricCalls()).toHaveLength(0); // batched …
      vi.advanceTimersByTime(3100); // … and flushed shortly after
      expect(metricCalls()).toHaveLength(1);
      const req = metricCalls()[0];
      expect(req.init.keepalive).toBe(true);
      expect(req.init.credentials).toBe('omit');
      expect((req.init.headers as Record<string, string>)['Authorization']).toBeUndefined();
      expect(JSON.parse(req.init.body as string)).toEqual({
        events: [{ ad_id: 'ad-1', kind: 'impression', region: 'DE' }],
      });
      // A click is flushed immediately.
      ads.click(ad);
      expect(metricCalls()).toHaveLength(2);
      expect(JSON.parse(metricCalls()[1].init.body as string).events[0]).toEqual({
        ad_id: 'ad-1',
        kind: 'click',
        region: 'DE',
      });
      expect(ads.stats().clicks).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('dismiss hides a creative on this device for a week and survives a restart', async () => {
    ads.init();
    await settle();
    ads.dismiss(CREATIVES[0]);
    expect(ads.current()?.id).toBe('ad-2');
    expect(ads.isDismissed('ad-1')).toBe(true);
    const stored = JSON.parse(localStorage.getItem('pa.ads.dismissed.v1')!);
    expect(stored['ad-1']).toBeGreaterThan(Date.now() + (DISMISS_DAYS - 1) * 86400_000);
    ads.dismiss(CREATIVES[1]);
    expect(ads.current()).toBeNull(); // nothing left to show: the panel simply disappears

    // A fresh service (new app start) still honours it.
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    const fresh = TestBed.inject(AdsService);
    expect(fresh.isDismissed('ad-1')).toBe(true);
    fresh.stop();
  });

  it('rotates only after the rotation time and only in calm moments', async () => {
    ads.init();
    await settle();
    const t0 = Date.now();
    expect(ads.current()?.id).toBe('ad-1');
    ads.tick(t0 + 60_000);
    expect(ads.current()?.id).toBe('ad-1'); // too early (300 s)
    ui.dialogOpened();
    ads.tick(t0 + 400_000);
    expect(ads.current()?.id).toBe('ad-1'); // a dialog is open
    ui.dialogClosed();
    ads.tick(t0 + 400_000);
    expect(ads.current()?.id).toBe('ad-2');
  });

  it('server kill switch and offline state hide the panel quietly', async () => {
    serveResponse = {
      ads: [],
      region: 'ZZ',
      rotation_seconds: 300,
      enabled: false,
      source: SPONSORED,
    };
    ads.init();
    await settle();
    expect(ads.serverEnabled()).toBe(false);
    expect(ads.eligible()).toBe(false);
    expect(ads.current()).toBeNull();

    serveResponse = {
      ads: CREATIVES,
      region: 'DE',
      rotation_seconds: 300,
      enabled: true,
      source: SPONSORED,
    };
    await ads.load();
    expect(ads.current()?.id).toBe('ad-1');
    ads.online.set(false);
    expect(ads.current()).toBeNull();
    ads.online.set(true);
    expect(ads.current()?.id).toBe('ad-1');
  });

  it('first-party source: label and disclosure come from the server, no consent involved', async () => {
    ads.init();
    await settle();
    expect(ads.source()?.kind).toBe('sponsored');
    expect(ads.badge()).toBe('Sponsored');
    expect(ads.embedMode()).toBe(false);
    expect(ads.embedAllowed()).toBe(false);
    expect(ads.consentState()).toBeUndefined();
  });

  it('third-party source: nothing may be embedded before consent; consent is device-local and revocable', async () => {
    serveResponse = {
      ads: [],
      region: 'DE',
      rotation_seconds: 300,
      enabled: true,
      source: ADSENSE,
    };
    ads.init();
    await settle();
    expect(ads.source()?.kind).toBe('adsense');
    expect(ads.badge()).toBe('Ad · Google AdSense');
    expect(ads.embedMode()).toBe(true);
    expect(ads.current()).toBeNull(); // no first-party creative; the panel shows the consent card
    expect(ads.embedAllowed()).toBe(false);
    expect(ads.consentState()).toBeUndefined();

    ads.setConsent('adsense', 'denied');
    expect(ads.consentState()).toBe('denied');
    expect(ads.embedAllowed()).toBe(false);

    ads.setConsent('adsense', 'granted');
    expect(ads.embedAllowed()).toBe(true);
    expect(JSON.parse(localStorage.getItem('pa.ads.consent.v1')!)).toEqual({ adsense: 'granted' });

    // Consent is per provider …
    serveResponse = {
      ...serveResponse,
      source: { ...ADSENSE, kind: 'microsoft', provider_name: 'Microsoft Advertising' },
    };
    await ads.load();
    expect(ads.embedAllowed()).toBe(false);
    // … survives a restart …
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    const fresh = TestBed.inject(AdsService);
    fresh.source.set(ADSENSE);
    expect(fresh.embedAllowed()).toBe(true);
    // … and can be withdrawn.
    fresh.withdrawConsent('adsense');
    expect(fresh.embedAllowed()).toBe(false);
    expect(fresh.consentState()).toBeUndefined();
    fresh.stop();
  });

  it('a failing ad server never surfaces to the user', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('boom');
      }),
    );
    ads.init();
    await settle();
    expect(ads.current()).toBeNull();
    expect(ads.lastError()).toBeTruthy();
    expect(ads.eligible()).toBe(true); // the strip/footer link still offers the controls
  });
});
