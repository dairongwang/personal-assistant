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
  /** Membership type resolved by the server (strategy key): free | paid | admin. */
  membership: MembershipType;
  membership_label: string;
  /** null = unlimited */
  max_active_tasks: number | null;
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

  async checkout(plan: 'pro_monthly' | 'pro_yearly' | 'lifetime'): Promise<string> {
    const res = await this.api.post<{ checkout_url: string }>('/billing/checkout', {
      plan,
      return_url: location.origin + '/?billing=done',
    });
    return res.checkout_url.startsWith('/')
      ? this.api.base.replace(/\/api\/v1$/, '') + res.checkout_url
      : res.checkout_url;
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
