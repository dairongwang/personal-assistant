import { Component, computed, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { AccountService } from '../account/account.service';
import { MembershipService } from '../membership/membership.service';
import { DialogDirective } from '../ui/dialog.directive';
import { TipDirective } from '../ui/tip.directive';
import { ToastService } from '../ui/toast.service';
import { relative } from '../update/update-policy';
import {
  AdminService,
  type AdminAccount,
  type AdminAccountDetail,
  type AdminStats,
  type MembershipGrant,
} from './admin.service';

/**
 * Administration panel (administrator membership only): service health numbers, account
 * search, and the support actions — grant/revoke Pro, promote, sign out everywhere, delete.
 * Everything shown is account metadata; the server has no content to show.
 */
@Component({
  selector: 'app-admin-dialog',
  imports: [FormsModule, DialogDirective, TipDirective],
  template: `
    <dialog appDialog id="admin-dialog" class="wide" aria-labelledby="admin-dialog-title">
      <form (ngSubmit)="search()">
        <h3 id="admin-dialog-title">Administration</h3>
        <p class="muted small">
          Signed in as {{ account.session()?.email }} (administrator). Actions are recorded in the
          audit log.
        </p>

        @if (stats(); as s) {
          <div class="update-summary admin-stats">
            <div>
              <span class="muted">Accounts</span
              ><strong>{{ s.accounts_total - s.accounts_deleted }}</strong>
            </div>
            <div>
              <span class="muted">Free / Pro / Admin</span
              ><strong
                >{{ s.memberships['free'] ?? 0 }} / {{ s.memberships['paid'] ?? 0 }} /
                {{ s.memberships['admin'] ?? 0 }}</strong
              >
            </div>
            <div>
              <span class="muted">Active devices</span><strong>{{ s.devices_active }}</strong>
            </div>
            <div>
              <span class="muted">Relay</span
              ><strong>{{ s.sync_ops }} ops · {{ kb(s.sync_bytes) }}</strong>
            </div>
            <div>
              <span class="muted">Vault snapshots</span
              ><strong>{{ s.snapshots }} · {{ kb(s.snapshot_bytes) }}</strong>
            </div>
            <div>
              <span class="muted">Service</span
              ><strong
                >v{{ s.service_version }} · {{ s.database }} · release
                {{ s.latest_release ?? '–' }}</strong
              >
            </div>
          </div>
        }
        @if (error()) {
          <div class="callout error" role="alert">
            <p>{{ error() }}</p>
          </div>
        }

        <div class="toolbar admin-toolbar">
          <input
            class="input search"
            id="admin-search"
            name="q"
            type="search"
            placeholder="Search by e-mail…"
            autocomplete="off"
            [(ngModel)]="query"
            appTip="Find accounts whose e-mail contains this text. Press Enter or Search."
          />
          <select
            class="input"
            name="membership"
            [(ngModel)]="filter"
            (ngModelChange)="search()"
            appTip="Show only accounts with this membership."
          >
            <option value="">All memberships</option>
            <option value="free">Free</option>
            <option value="paid">Pro</option>
            <option value="admin">Administrators</option>
          </select>
          <!-- Never disabled: a disabled default button would also swallow Enter in the search field. -->
          <button class="btn" type="submit" appTip="Run the search.">Search</button>
        </div>

        <ul class="admin-list" id="admin-accounts">
          @for (a of accounts(); track a.id) {
            <li [class.selected]="selected()?.id === a.id">
              <button
                type="button"
                class="admin-row"
                (click)="select(a)"
                [attr.aria-expanded]="selected()?.id === a.id"
                appTip="Show details and support actions for this account."
              >
                <span class="admin-email">{{ a.email }}</span>
                <span class="admin-meta">
                  <span class="badge" [class]="'badge membership ' + a.membership">{{
                    membershipLabel(a.membership)
                  }}</span>
                  <span class="muted small"
                    >{{ a.plan }} · {{ a.status }} · {{ a.devices_active }} device{{
                      a.devices_active === 1 ? '' : 's'
                    }}
                    · seen {{ a.last_seen_at ? rel(a.last_seen_at) : 'never' }}</span
                  >
                </span>
              </button>
              @if (selected()?.id === a.id && detail(); as d) {
                <div class="admin-detail">
                  <div class="update-summary">
                    <div>
                      <span class="muted">Membership</span
                      ><strong>{{ membershipLabel(d.membership) }} ({{ d.role }})</strong>
                    </div>
                    <div>
                      <span class="muted">Billing</span
                      ><span
                        >{{ d.plan }} · {{ d.status }} · {{ d.provider
                        }}{{
                          d.current_period_end ? ' · until ' + date(d.current_period_end) : ''
                        }}</span
                      >
                    </div>
                    <div>
                      <span class="muted">Active tasks allowed</span
                      ><span>{{ d.max_active_tasks ?? 'unlimited' }}</span>
                    </div>
                    <div>
                      <span class="muted">Relay / vault</span
                      ><span
                        >{{ d.sync_ops }} ops · {{ kb(d.sync_bytes) }} ·
                        {{ d.snapshot_bytes === null ? 'no snapshot' : kb(d.snapshot_bytes) }}</span
                      >
                    </div>
                    <div>
                      <span class="muted">Created</span><span>{{ date(d.created_at) }}</span>
                    </div>
                    <div>
                      <span class="muted">Devices</span
                      ><span>
                        @for (dev of d.devices; track dev.id) {
                          <span class="chip"
                            >{{ dev.platform }} v{{ dev.client_version || '?' }} ·
                            {{ rel(dev.last_seen_at) }}</span
                          >
                        } @empty {
                          none
                        }
                      </span>
                    </div>
                  </div>
                  <div class="dialog-actions admin-actions">
                    <button
                      class="btn"
                      type="button"
                      (click)="grant(d, { plan: 'pro', days: 30 })"
                      [disabled]="busy()"
                      appTip="Give this account Pro for 30 days (comp, refund goodwill, support case). Unlimited tasks and sync turn on immediately."
                    >
                      Grant Pro 30 d
                    </button>
                    <button
                      class="btn"
                      type="button"
                      (click)="grant(d, { plan: 'lifetime' })"
                      [disabled]="busy()"
                      appTip="Give this account a Lifetime membership."
                    >
                      Grant lifetime
                    </button>
                    <button
                      class="btn"
                      type="button"
                      (click)="grant(d, { plan: 'free' })"
                      [disabled]="busy() || d.membership === 'free'"
                      appTip="Take the paid membership away: back to the Free limits (3 active tasks, no sync). Data on the user's devices is untouched."
                    >
                      Set free
                    </button>
                    <button
                      class="btn"
                      type="button"
                      (click)="toggleRole(d)"
                      [disabled]="busy() || d.id === account.session()?.accountId"
                      appTip="Administrators get the admin panel and every Pro feature. You cannot change your own role."
                    >
                      {{ d.role === 'admin' ? 'Remove admin' : 'Make admin' }}
                    </button>
                    <button
                      class="btn"
                      type="button"
                      (click)="signOut(d)"
                      [disabled]="busy() || d.devices_active === 0"
                      appTip="Sign the account out on every device (lost phone, leaked token). The user signs in again with their password."
                    >
                      Sign out everywhere
                    </button>
                    <button
                      class="btn danger"
                      type="button"
                      (click)="remove(d)"
                      [disabled]="busy() || d.id === account.session()?.accountId"
                      appTip="Delete the account on the server (relay data, devices, e-mail). Data on the user's devices stays. Cannot be undone."
                    >
                      Delete
                    </button>
                  </div>
                  @if (d.audit.length) {
                    <details class="small">
                      <summary class="muted">Audit ({{ d.audit.length }})</summary>
                      <ul class="device-list">
                        @for (e of d.audit; track e.at + e.action) {
                          <li>
                            <span
                              >{{ rel(e.at) }} · {{ e.action }} · {{ e.actor_email ?? 'system' }}
                              {{ note(e.detail) }}</span
                            >
                          </li>
                        }
                      </ul>
                    </details>
                  }
                </div>
              }
            </li>
          } @empty {
            <li class="empty">{{ loaded() ? 'No accounts match.' : 'Loading…' }}</li>
          }
        </ul>
        @if (total() > accounts().length) {
          <p class="muted small">
            Showing {{ accounts().length }} of {{ total() }} — narrow the search.
          </p>
        }

        <div class="dialog-actions">
          <button
            class="btn"
            type="button"
            (click)="refresh()"
            [disabled]="busy()"
            appTip="Reload the numbers and the list."
          >
            Refresh
          </button>
          @if (stats()?.ads_admin_url; as url) {
            <a
              class="btn"
              id="link-ads-admin"
              [href]="url"
              target="_blank"
              rel="noopener noreferrer"
              appTip="Advertising (campaigns, ad source, analytics, privacy & compliance) is managed in the separate ads admin console. Opens in a new tab; sign in there with your administrator account."
            >
              Ads admin console ↗
            </a>
          }
          <span class="spacer"></span>
          <button class="btn" type="button" (click)="dialog().close()" appTip="Close this window.">
            Close
          </button>
        </div>
      </form>
    </dialog>
  `,
})
export class AdminDialog {
  readonly account = inject(AccountService);
  readonly membership = inject(MembershipService);
  private readonly admin = inject(AdminService);
  private readonly toast = inject(ToastService);
  readonly dialog = viewChild.required(DialogDirective);

  readonly stats = signal<AdminStats | null>(null);
  readonly accounts = signal<AdminAccount[]>([]);
  readonly total = signal(0);
  readonly selected = signal<AdminAccount | null>(null);
  readonly detail = signal<AdminAccountDetail | null>(null);
  readonly busy = signal(false);
  readonly loaded = signal(false);
  readonly error = signal<string | null>(null);
  query = '';
  filter: '' | 'free' | 'paid' | 'admin' = '';

  readonly isAdmin = computed(() => this.membership.isAdmin());

  open() {
    this.error.set(null);
    this.dialog().open();
    void this.refresh();
  }

  async refresh() {
    await this.guard(async () => {
      this.stats.set(await this.admin.stats());
      await this.search();
      const sel = this.selected();
      if (sel) this.detail.set(await this.admin.account(sel.id));
    });
  }

  private searchSeq = 0;

  async search() {
    const seq = ++this.searchSeq; // a newer search supersedes this one: ignore a late answer
    await this.guard(async () => {
      const page = await this.admin.accounts(this.query.trim(), this.filter);
      if (seq !== this.searchSeq) return;
      this.accounts.set(page.items);
      this.total.set(page.total);
      this.loaded.set(true);
    });
  }

  async select(a: AdminAccount) {
    if (this.selected()?.id === a.id) {
      this.selected.set(null);
      this.detail.set(null);
      return;
    }
    this.selected.set(a);
    this.detail.set(null);
    await this.guard(async () => this.detail.set(await this.admin.account(a.id)));
  }

  async grant(d: AdminAccountDetail, grant: MembershipGrant) {
    const note = prompt('Note for the audit log (optional):', '') ?? '';
    await this.apply(
      d,
      () => this.admin.setMembership(d.id, { ...grant, note }),
      'Membership updated',
    );
  }

  async toggleRole(d: AdminAccountDetail) {
    const role = d.role === 'admin' ? 'user' : 'admin';
    if (
      !confirm(
        `${role === 'admin' ? 'Make' : 'Remove'} ${d.email} ${role === 'admin' ? 'an administrator' : 'as administrator'}?`,
      )
    )
      return;
    await this.apply(d, () => this.admin.setRole(d.id, role), 'Role updated');
  }

  async signOut(d: AdminAccountDetail) {
    if (!confirm(`Sign ${d.email} out on all ${d.devices_active} device(s)?`)) return;
    await this.apply(d, () => this.admin.signOutEverywhere(d.id), 'Signed out everywhere');
  }

  async remove(d: AdminAccountDetail) {
    if (!confirm(`Delete the account ${d.email} on the server? This cannot be undone.`)) return;
    await this.guard(async () => {
      await this.admin.deleteAccount(d.id);
      this.selected.set(null);
      this.detail.set(null);
      this.toast.show('Account deleted');
      await this.refresh();
    });
  }

  private async apply(
    d: AdminAccountDetail,
    action: () => Promise<AdminAccountDetail>,
    message: string,
  ) {
    await this.guard(async () => {
      const updated = await action();
      this.detail.set(updated);
      this.accounts.update((list) =>
        list.map((a) => (a.id === d.id ? { ...a, ...summary(updated) } : a)),
      );
      this.toast.show(message);
      this.stats.set(await this.admin.stats());
    });
  }

  private async guard(fn: () => Promise<void>) {
    this.busy.set(true);
    this.error.set(null);
    try {
      await fn();
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.busy.set(false);
    }
  }

  membershipLabel(type: string) {
    return { anonymous: 'Free', free: 'Free', paid: 'Pro', admin: 'Admin' }[type] ?? type;
  }
  rel(iso: string) {
    return relative(new Date(iso));
  }
  date(iso: string) {
    return new Date(iso).toLocaleDateString();
  }
  kb(bytes: number) {
    return bytes < 1024
      ? `${bytes} B`
      : bytes < 1024 * 1024
        ? `${(bytes / 1024).toFixed(1)} KB`
        : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }
  note(detail: Record<string, unknown>) {
    const parts: string[] = [];
    const after = detail['after'] as { plan?: string; status?: string } | string | undefined;
    if (after && typeof after === 'object') parts.push(`→ ${after.plan} ${after.status}`);
    else if (typeof after === 'string') parts.push(`→ ${after}`);
    if (typeof detail['note'] === 'string' && detail['note']) parts.push(`“${detail['note']}”`);
    if (typeof detail['devices'] === 'number') parts.push(`${detail['devices']} device(s)`);
    return parts.join(' ');
  }
}

function summary(d: AdminAccountDetail): AdminAccount {
  const {
    id,
    email,
    role,
    membership,
    plan,
    status,
    current_period_end,
    created_at,
    deleted_at,
    devices_active,
    last_seen_at,
  } = d;
  return {
    id,
    email,
    role,
    membership,
    plan,
    status,
    current_period_end,
    created_at,
    deleted_at,
    devices_active,
    last_seen_at,
  };
}
