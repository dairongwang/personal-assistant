/* API client of the ad-admin dashboard: administrator sign-in (the same client-side key
 * derivation as the user app — the password never leaves the browser) and the /admin/ads/*
 * endpoints. The session token lives in sessionStorage (gone when the tab closes). */

const TOKEN_KEY = 'adsadmin.token';
const EMAIL_KEY = 'adsadmin.email';
const enc = new TextEncoder();

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export function apiBase() {
  const cfg = (typeof window !== 'undefined' && window.ADS_ADMIN_CONFIG) || {};
  return String(cfg.apiBase || '').replace(/\/$/, '') + '/api/v1';
}

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');

/** PBKDF2-SHA256(600k) → HKDF("auth"): identical to apps/web crypto.ts and docs_gate.py. */
export async function deriveAuthKey(password, email, iterations = 600000) {
  const salt = await crypto.subtle.digest('SHA-256', enc.encode(email.trim().toLowerCase()));
  const base = await crypto.subtle.importKey('raw', enc.encode(password.normalize('NFKC')), 'PBKDF2', false, [
    'deriveBits',
  ]);
  const master = await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations }, base, 256);
  const hkdf = await crypto.subtle.importKey('raw', master, 'HKDF', false, ['deriveBits']);
  return hex(
    await crypto.subtle.deriveBits(
      { name: 'HKDF', hash: 'SHA-256', salt: new Uint8Array(32), info: enc.encode('auth') },
      hkdf,
      256,
    ),
  );
}

export const session = {
  get token() {
    return sessionStorage.getItem(TOKEN_KEY);
  },
  get email() {
    return sessionStorage.getItem(EMAIL_KEY);
  },
  set(token, email) {
    sessionStorage.setItem(TOKEN_KEY, token);
    sessionStorage.setItem(EMAIL_KEY, email);
  },
  clear() {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(EMAIL_KEY);
  },
};

async function request(method, path, body, { auth = true, raw = false } = {}) {
  const headers = { Accept: raw ? '*/*' : 'application/json', 'X-Client-Version': 'ads-admin' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (auth && session.token) headers['Authorization'] = `Bearer ${session.token}`;
  let res;
  try {
    res = await fetch(apiBase() + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
      credentials: 'omit',
    });
  } catch {
    throw new ApiError(`The API at ${apiBase()} cannot be reached (offline, CORS, or wrong apiBase in config.js).`, 0);
  }
  if (res.status === 204) return null;
  if (raw) {
    if (!res.ok) throw new ApiError(`Request failed (${res.status})`, res.status);
    return res.text();
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* no body */
  }
  if (!res.ok) {
    const detail = data && data.detail;
    const message =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ')
          : `Request failed (${res.status})`;
    if (res.status === 401 && auth) session.clear();
    throw new ApiError(message, res.status, data);
  }
  return data;
}

export const api = {
  async login(email, password) {
    const auth_key = await deriveAuthKey(password, email);
    const s = await request(
      'POST',
      '/auth/login',
      { email, auth_key, device: { platform: 'ads-admin', client_version: 'ads-admin' } },
      { auth: false },
    );
    session.set(s.token, s.email);
    const me = await request('GET', '/me');
    if (me.role !== 'admin') {
      await this.logout();
      throw new ApiError('This dashboard needs an administrator account (role = admin).', 403);
    }
    return me;
  },
  async logout() {
    try {
      await request('POST', '/auth/logout', {});
    } catch {
      /* token already invalid */
    }
    session.clear();
  },
  me: () => request('GET', '/me'),
  health: () => request('GET', '/health', undefined, { auth: false }),
  analytics: (days, kind = '') => request('GET', `/admin/ads/analytics?days=${days}${kind ? `&kind=${kind}` : ''}`),
  analyticsCsv: (days) => request('GET', `/admin/ads/analytics.csv?days=${days}`, undefined, { raw: true }),
  compliance: () => request('GET', '/admin/ads/compliance'),
  overview: () => request('GET', '/admin/ads/overview'),
  sources: () => request('GET', '/admin/ads/sources'),
  setSource: (kind) => request('PUT', '/admin/ads/sources', { kind }),
  campaigns: (includeArchived) => request('GET', `/admin/ads${includeArchived ? '?include_archived=true' : ''}`),
  campaign: (id) => request('GET', `/admin/ads/${id}`),
  createCampaign: (body) => request('POST', '/admin/ads', body),
  updateCampaign: (id, patch) => request('PATCH', `/admin/ads/${id}`, patch),
  archiveCampaign: (id) => request('DELETE', `/admin/ads/${id}`),
  campaignStats: (id, days) => request('GET', `/admin/ads/${id}/stats?days=${days}`),
  audit: (limit = 200) => request('GET', `/admin/audit?limit=${limit}`),
  // Payments (admin payment analytics): `query` is the string from payments.filterQuery().
  paymentAnalytics: (query = '') => request('GET', `/admin/payments/analytics${query}`),
  paymentTransactions: (query = '') => request('GET', `/admin/payments/transactions${query}`),
  paymentTransaction: (id) => request('GET', `/admin/payments/transactions/${encodeURIComponent(id)}`),
  paymentTransactionsCsv: (query = '') =>
    request('GET', `/admin/payments/transactions.csv${query}`, undefined, { raw: true }),
};
