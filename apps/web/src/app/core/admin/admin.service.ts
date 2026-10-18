import { Injectable, inject } from '@angular/core';
import { ApiService } from '../account/api.service';
import type { MembershipType } from '../membership/membership';

export interface AdminStats {
  accounts_total: number;
  accounts_deleted: number;
  memberships: Partial<Record<'free' | 'paid' | 'admin', number>>;
  subscriptions: Record<string, number>;
  devices_active: number;
  sync_ops: number;
  sync_bytes: number;
  snapshots: number;
  snapshot_bytes: number;
  latest_release: string | null;
  service_version: string;
  database: string;
}

export interface AdminAccount {
  id: string;
  email: string;
  role: 'user' | 'admin';
  membership: MembershipType;
  plan: string;
  status: string;
  current_period_end: string | null;
  created_at: string;
  deleted_at: string | null;
  devices_active: number;
  last_seen_at: string | null;
}

export interface AdminAuditEntry {
  at: string;
  actor_account_id: string | null;
  actor_email: string | null;
  action: string;
  detail: Record<string, unknown>;
}

export interface AdminAccountDetail extends AdminAccount {
  vault_enabled: boolean;
  provider: string;
  cancel_at_period_end: boolean;
  features: string[];
  max_active_tasks: number | null;
  devices: {
    id: string;
    platform: string;
    client_version: string;
    schema_version: number;
    cursor: number;
    created_at: string;
    last_seen_at: string;
  }[];
  sync_ops: number;
  sync_bytes: number;
  snapshot_bytes: number | null;
  audit: AdminAuditEntry[];
}

export interface AdminAccountPage {
  items: AdminAccount[];
  total: number;
  offset: number;
  limit: number;
}

export type MembershipGrant =
  | { plan: 'pro'; days?: number; note?: string }
  | { plan: 'lifetime'; note?: string }
  | { plan: 'free'; status?: 'none' | 'canceled'; note?: string };

/** Administrator API (production support). Only reachable with an admin account (403 otherwise). */
@Injectable({ providedIn: 'root' })
export class AdminService {
  private readonly api = inject(ApiService);

  stats() {
    return this.api.get<AdminStats>('/admin/stats');
  }

  accounts(q = '', membership: MembershipType | '' = '', includeDeleted = false, limit = 50) {
    const params = new URLSearchParams({ q, membership, limit: String(limit) });
    if (includeDeleted) params.set('include_deleted', 'true');
    return this.api.get<AdminAccountPage>(`/admin/accounts?${params}`);
  }

  account(id: string) {
    return this.api.get<AdminAccountDetail>(`/admin/accounts/${id}`);
  }

  setMembership(id: string, grant: MembershipGrant) {
    return this.api.patch<AdminAccountDetail>(`/admin/accounts/${id}/membership`, grant);
  }

  setRole(id: string, role: 'user' | 'admin') {
    return this.api.patch<AdminAccountDetail>(`/admin/accounts/${id}/role`, { role });
  }

  signOutEverywhere(id: string) {
    return this.api.post<AdminAccountDetail>(`/admin/accounts/${id}/sign-out`);
  }

  deleteAccount(id: string) {
    return this.api.delete<void>(`/admin/accounts/${id}`);
  }

  audit(limit = 100) {
    return this.api.get<AdminAuditEntry[]>(`/admin/audit?limit=${limit}`);
  }
}
