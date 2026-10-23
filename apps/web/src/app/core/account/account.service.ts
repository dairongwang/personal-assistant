import { Injectable, computed, inject, signal } from '@angular/core';
import { DbService } from '../db/db.service';
import type { MembershipType } from '../membership/membership';
import { RELEASE } from '../release-info';
import { ApiError, ApiService } from './api.service';
import {
  KDF_PARAMS,
  deriveKeys,
  generateDataKey,
  unwrapDataKey,
  wrapDataKey,
} from '../sync/crypto';

export interface Session {
  token: string;
  account_id: string;
  device_id: string;
  email: string;
  wrapped_data_key: string;
  kdf_params: { kdf: string; iterations: number };
  vault_enabled: boolean;
}

export interface Entitlements {
  plan: string;
  status: string;
  features: string[];
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  trial_available: boolean;
  /** Payment strategy holding the subscription: stripe | paypal | seed | admin | … */
  provider: string;
  /** Until when the paid membership lasts as things stand (null = Lifetime / nothing paid). */
  access_until: string | null;
  /** Renews automatically at access_until. */
  renews: boolean;
  /** POST /billing/cancel ends the membership now and refunds (GET /billing/cancel/preview). */
  can_cancel: boolean;
  /** The provider has a self-service page for this subscription (POST /billing/manage). */
  can_manage: boolean;
  /** The most recent charge — paid, failed or refunded. */
  last_payment: PaymentOut | null;
  /** Membership type resolved by the server (strategy key): free | paid | admin. */
  membership: MembershipType;
  membership_label: string;
  /** null = unlimited */
  max_active_tasks: number | null;
}

export type Plan = 'pro_monthly' | 'pro_yearly' | 'lifetime';

/** One way of paying, as the server's payment strategies describe themselves (/billing/methods). */
export interface PaymentMethod {
  /** Strategy key: stripe | paypal */
  name: string;
  label: string;
  description: string;
  plans: Plan[];
  /** Nothing is charged (the provider's test mode or the built-in fake). */
  sandbox: boolean;
  /** The server's built-in fake of this provider answers (no credentials configured). */
  fake: boolean;
  /** False = the server has no credentials for it yet (checkout answers 503 with the reason). */
  available: boolean;
}

/** One paid membership as sold (price from the server's PRICE_* settings, refund rule). */
export interface PlanInfo {
  id: Plan;
  title: string;
  /** Decimal string, e.g. "1.00" */
  amount: string;
  amount_cents: number;
  currency: string;
  interval: 'month' | 'year' | null;
  period_days: number | null;
  refund_rule: string;
}

export interface PaymentMethods {
  methods: PaymentMethod[];
  default: string | null;
  plans: PlanInfo[];
  currency: string;
}

/** One charge from the ledger: what was paid, failed or refunded. */
export interface PaymentOut {
  id: number;
  provider: string;
  plan: Plan;
  amount: string;
  currency: string;
  status: 'succeeded' | 'failed' | 'refunded' | 'partially_refunded';
  refunded: string;
  refund_id: string | null;
  failure_reason: string | null;
  period_start: string | null;
  period_end: string | null;
  paid_at: string;
}

/** What cancelling right now gives back. */
export interface RefundPreview {
  amount: string;
  amount_cents: number;
  currency: string;
  basis: 'prorated' | 'flat' | 'none';
  days_left: number | null;
  days_total: number | null;
  paid: string | null;
  note: string;
}

export interface CancelResult {
  entitlements: Entitlements;
  refund: RefundPreview;
  refund_id: string | null;
  refunded_via: 'provider' | 'recorded' | 'none';
}

export interface DeviceOut {
  id: string;
  platform: string;
  client_version: string;
  schema_version: number;
  cursor: number;
  last_seen_at: string;
  is_current: boolean;
}

export interface Me {
  account_id: string;
  email: string;
  role: 'user' | 'admin';
  created_at: string;
  vault_enabled: boolean;
  devices: DeviceOut[];
  entitlements: Entitlements;
}

export interface StoredSession {
  accountId: string;
  deviceId: string;
  email: string;
  vaultEnabled: boolean;
  /** Last membership the server reported — applies while offline (MembershipService). */
  membership?: MembershipType;
  maxActiveTasks?: number | null;
}

function platformName(): string {
  const ua = navigator.userAgent;
  if (/iPhone|iPad|iPod/.test(ua)) return 'ios-web';
  if (/Android/.test(ua)) return 'android-web';
  if (/Windows/.test(ua)) return 'windows-web';
  if (/Mac OS/.test(ua)) return 'macos-web';
  if (/Linux/.test(ua)) return 'linux-web';
  return 'web';
}

/**
 * Optional account: everything works without one. With one, the device holds a non-extractable
 * data key (IndexedDB) and an access token; the server knows the e-mail, a verifier, the
 * wrapped key and the plan — nothing else.
 */
@Injectable({ providedIn: 'root' })
export class AccountService {
  private readonly api = inject(ApiService);
  private readonly dbs = inject(DbService);

  readonly session = signal<StoredSession | null>(null);
  readonly dataKey = signal<CryptoKey | null>(null);
  readonly me = signal<Me | null>(null);
  readonly ready = signal(false);
  readonly busy = signal(false);

  readonly signedIn = computed(() => !!this.session() && !!this.dataKey());
  readonly entitlements = computed(() => this.me()?.entitlements ?? null);
  readonly canSync = computed(
    () => this.signedIn() && (this.entitlements()?.features.includes('sync') ?? false),
  );
  readonly vaultEnabled = computed(
    () => this.me()?.vault_enabled ?? this.session()?.vaultEnabled ?? true,
  );

  async init(): Promise<void> {
    const db = this.dbs.db;
    const [session, key] = await Promise.all([
      db.getMeta<StoredSession>('session'),
      db.getMeta<CryptoKey>('dataKey'),
    ]);
    if (session && key && this.api.token()) {
      this.session.set(session);
      this.dataKey.set(key);
      void this.refreshMe();
    }
    // Membership can change behind the app's back (subscription renewed/lapsed, an administrator
    // granted Pro): re-read /me when the user returns to the tab, at most once a minute.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden || !this.signedIn()) return;
      if (Date.now() - this.lastRefresh < 60_000) return;
      void this.refreshMe();
    });
    this.ready.set(true);
  }

  private lastRefresh = 0;

  async refreshMe(): Promise<Me | null> {
    if (!this.api.token()) return null;
    this.lastRefresh = Date.now();
    try {
      const me = await this.api.get<Me>('/me');
      this.me.set(me);
      await this.rememberMembership(me);
      return me;
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) await this.forget();
      return null;
    }
  }

  /** Cache the membership with the session so the right limits apply when the app starts offline. */
  private async rememberMembership(me: Me): Promise<void> {
    const s = this.session();
    if (!s || s.accountId !== me.account_id) return;
    const next: StoredSession = {
      ...s,
      email: me.email,
      vaultEnabled: me.vault_enabled,
      membership: me.entitlements.membership,
      maxActiveTasks: me.entitlements.max_active_tasks,
    };
    if (
      next.membership !== s.membership ||
      next.maxActiveTasks !== s.maxActiveTasks ||
      next.vaultEnabled !== s.vaultEnabled
    ) {
      this.session.set(next);
      await this.dbs.db.setMeta('session', next);
    }
  }

  private deviceInfo() {
    return {
      platform: platformName(),
      client_version: RELEASE.version,
      schema_version: RELEASE.schemaVersion,
    };
  }

  async register(email: string, password: string): Promise<void> {
    this.busy.set(true);
    try {
      const keys = await deriveKeys(password, email);
      const dataKey = await generateDataKey();
      const wrapped = await wrapDataKey(dataKey, keys.wrapKey);
      const session = await this.api.post<Session>(
        '/auth/register',
        {
          email,
          auth_key: keys.authKey,
          wrapped_data_key: wrapped,
          kdf_params: KDF_PARAMS,
          device: this.deviceInfo(),
        },
        { auth: false },
      );
      // Re-import as non-extractable for storage.
      await this.adopt(session, await unwrapDataKey(wrapped, keys.wrapKey));
    } finally {
      this.busy.set(false);
    }
  }

  async login(email: string, password: string): Promise<void> {
    this.busy.set(true);
    try {
      const keys = await deriveKeys(password, email); // default params; re-derive if the account pins others
      const session = await this.api.post<Session>(
        '/auth/login',
        { email, auth_key: keys.authKey, device: this.deviceInfo() },
        { auth: false },
      );
      let wrapKey = keys.wrapKey;
      if (
        session.kdf_params?.iterations &&
        session.kdf_params.iterations !== KDF_PARAMS.iterations
      ) {
        wrapKey = (await deriveKeys(password, email, session.kdf_params)).wrapKey;
      }
      await this.adopt(session, await unwrapDataKey(session.wrapped_data_key, wrapKey));
    } finally {
      this.busy.set(false);
    }
  }

  private async adopt(session: Session, dataKey: CryptoKey) {
    const stored: StoredSession = {
      accountId: session.account_id,
      deviceId: session.device_id,
      email: session.email,
      vaultEnabled: session.vault_enabled,
    };
    this.api.setToken(session.token);
    await this.dbs.db.setMeta('session', stored);
    await this.dbs.db.setMeta('dataKey', dataKey);
    this.session.set(stored);
    this.dataKey.set(dataKey);
    await this.refreshMe();
  }

  async logout(): Promise<void> {
    try {
      await this.api.post('/auth/logout');
    } catch {
      /* offline: the token is dropped locally; the server revokes it on expiry */
    }
    await this.forget();
  }

  async forget(): Promise<void> {
    this.api.setToken(null);
    await this.dbs.db.meta.bulkDelete(['session', 'dataKey', 'cursor', 'lastSyncAt']);
    this.session.set(null);
    this.dataKey.set(null);
    this.me.set(null);
  }

  async startTrial(): Promise<Entitlements> {
    const ent = await this.api.post<Entitlements>('/billing/trial');
    await this.refreshMe();
    return ent;
  }

  /** The payment methods the server offers right now (none = billing is not configured). */
  async paymentMethods(): Promise<PaymentMethods> {
    return this.api.get<PaymentMethods>('/billing/methods', { auth: false });
  }

  /** Starts a hosted checkout with the chosen payment method (undefined = the server's default)
   *  and returns the page to send the browser to. */
  async checkout(plan: Plan, method?: string): Promise<string> {
    const res = await this.api.post<{ checkout_url: string }>('/billing/checkout', {
      plan,
      method: method ?? null,
      return_url: location.origin + '/?billing=done',
    });
    return this.absolute(res.checkout_url);
  }

  /** What cancelling right now would refund (shown before asking for confirmation). */
  async cancelPreview(): Promise<RefundPreview> {
    return this.api.get<RefundPreview>('/billing/cancel/preview');
  }

  /** Ends the paid membership now and refunds (unused days of the period / the flat Lifetime amount). */
  async cancelSubscription(): Promise<CancelResult> {
    const result = await this.api.post<CancelResult>('/billing/cancel');
    await this.refreshMe();
    return result;
  }

  /** This account's charges, failures and refunds, newest first. */
  async payments(): Promise<PaymentOut[]> {
    return this.api.get<PaymentOut[]>('/billing/payments');
  }

  /** The provider's self-service page (payment method, invoices) for this subscription. */
  async manageUrl(): Promise<string> {
    const res = await this.api.post<{ url: string }>('/billing/manage', {
      return_url: location.origin + '/?billing=managed',
    });
    return this.absolute(res.url);
  }

  /** Pages the API hosts itself (built-in provider fakes) come back as paths on the API origin. */
  private absolute(url: string): string {
    return url.startsWith('/') ? this.api.base.replace(/\/api\/v1$/, '') + url : url;
  }

  async setVault(enabled: boolean): Promise<void> {
    const me = await this.api.patch<Me>('/me/settings', { vault_enabled: enabled });
    this.me.set(me);
    const s = this.session();
    if (s) {
      const next = { ...s, vaultEnabled: enabled };
      this.session.set(next);
      await this.dbs.db.setMeta('session', next);
    }
  }

  async revokeDevice(id: string): Promise<void> {
    await this.api.delete(`/me/devices/${id}`);
    await this.refreshMe();
  }

  async deleteAccount(): Promise<void> {
    await this.api.delete('/me');
    await this.forget();
  }
}
