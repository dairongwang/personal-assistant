import { Injectable, inject, signal } from '@angular/core';
import { RELEASE } from '../release-info';
import { SettingsService } from '../settings/settings.service';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown = null,
  ) {
    super(message);
  }
  get offline() {
    return this.status === 0;
  }
  get upgradeRequired() {
    return this.status === 426;
  }
}

/**
 * Thin fetch wrapper for the Account & Sync API. Sends the app's version/schema on every call
 * so the server can refuse clients that are too old (426 → forced update, see UpdateService).
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly settings = inject(SettingsService);
  readonly base = this.resolveBase();
  readonly token = signal<string | null>(localStorage.getItem('pa.token'));
  /** Set when any call answered 426; the update service reacts to it. */
  readonly upgradeRequired = signal<string | null>(null);

  private resolveBase(): string {
    const meta = document.querySelector('meta[name="api-base"]')?.getAttribute('content')?.trim();
    const override = localStorage.getItem('pa.apiBase')?.trim();
    return (override || meta || '').replace(/\/$/, '') + '/api/v1';
  }

  setToken(token: string | null) {
    this.token.set(token);
    if (token) localStorage.setItem('pa.token', token);
    else localStorage.removeItem('pa.token');
  }

  async request<T>(
    method: string,
    path: string,
    body?: unknown,
    opts: { auth?: boolean } = {},
  ): Promise<T> {
    const headers: Record<string, string> = {
      'X-Client-Version': RELEASE.version,
      'X-Schema-Version': String(RELEASE.schemaVersion),
      Accept: 'application/json',
    };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const token = this.token();
    if (opts.auth !== false && token) headers['Authorization'] = `Bearer ${token}`;
    let res: Response;
    try {
      res = await fetch(this.base + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: 'no-store',
      });
    } catch {
      throw new ApiError("You're offline, or the server can't be reached right now.", 0);
    }
    if (res.status === 204) return undefined as T;
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      /* no body */
    }
    if (!res.ok) {
      const message = describe(data, res.status);
      if (res.status === 426) this.upgradeRequired.set(message);
      if (res.status === 401 && opts.auth !== false && token) this.setToken(null);
      throw new ApiError(message, res.status, data);
    }
    return data as T;
  }

  get<T>(path: string, opts?: { auth?: boolean }) {
    return this.request<T>('GET', path, undefined, opts);
  }
  post<T>(path: string, body?: unknown, opts?: { auth?: boolean }) {
    return this.request<T>('POST', path, body ?? {}, opts);
  }
  put<T>(path: string, body: unknown) {
    return this.request<T>('PUT', path, body);
  }
  patch<T>(path: string, body: unknown) {
    return this.request<T>('PATCH', path, body);
  }
  delete<T>(path: string) {
    return this.request<T>('DELETE', path);
  }

  channel() {
    return this.settings.settings().channel;
  }
}

function describe(data: unknown, status: number): string {
  const detail = (data as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => `${((d.loc as string[]) ?? []).slice(1).join('.')}: ${d.msg}`.replace(/^: /, ''))
      .join('; ');
  }
  if (status === 503) return 'The server is temporarily unavailable. Please try again in a moment.';
  if (status >= 500) return 'The server ran into a problem. Please try again later.';
  return `Request failed (${status})`;
}
