import { Component, computed, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { TasksService } from '../data/tasks.service';
import { MembershipService } from '../membership/membership.service';
import { SyncService } from '../sync/sync.service';
import { DialogDirective } from '../ui/dialog.directive';
import { TipDirective } from '../ui/tip.directive';
import { ToastService } from '../ui/toast.service';
import { relative } from '../update/update-policy';
import { AccountService } from './account.service';
import { ApiError } from './api.service';

@Component({
  selector: 'app-account-dialog',
  imports: [FormsModule, DialogDirective, TipDirective],
  template: `
    <dialog appDialog id="account-dialog" aria-labelledby="account-dialog-title">
      <form (ngSubmit)="submit()">
        <h3 id="account-dialog-title">
          {{
            account.signedIn()
              ? 'Account & sync'
              : mode() === 'register'
                ? 'Create account'
                : 'Sign in'
          }}
        </h3>

        @if (!account.signedIn()) {
          <p class="muted small">
            You don't need an account — everything already works on this device. An account adds
            end-to-end-encrypted sync between your devices (Pro). Your password never leaves this
            device; the server only stores an encrypted copy of your data that it cannot read.
          </p>
          <div class="update-summary membership-table" id="membership-table">
            <div>
              <span class="muted">Free (with or without account)</span>
              <strong>{{ freeLimitLabel() }}</strong>
              <span class="small">{{ tasks.usageLabel() }} on this device</span>
            </div>
            <div>
              <span class="muted">Pro</span>
              <strong>Unlimited active tasks</strong>
              <span class="small">+ encrypted sync & backup across devices</span>
            </div>
          </div>
          <div class="field">
            <label for="acc-email">E-mail</label>
            <input
              class="input"
              id="acc-email"
              name="email"
              type="email"
              [(ngModel)]="email"
              required
              autocomplete="email"
              appTip="Used to sign in and for receipts. Nothing else is stored about you."
            />
          </div>
          <div class="field">
            <label for="acc-password">Password</label>
            <input
              class="input"
              id="acc-password"
              name="password"
              type="password"
              [(ngModel)]="password"
              required
              minlength="8"
              autocomplete="current-password"
              appTip="Your encryption key is derived from this password on the device. If you lose it, synced data cannot be recovered by anyone."
            />
          </div>
          @if (error()) {
            <div class="callout error" role="alert">
              <p>{{ error() }}</p>
            </div>
          }
          <div class="dialog-actions">
            <button
              class="btn ghost"
              type="button"
              (click)="mode.set(mode() === 'register' ? 'login' : 'register')"
              appTip="Switch between signing in and creating a new account."
            >
              {{ mode() === 'register' ? 'I already have an account' : 'Create a new account' }}
            </button>
            <span class="spacer"></span>
            <button
              class="btn"
              type="button"
              (click)="dialog().close()"
              appTip="Close this window."
            >
              Close
            </button>
            <button
              class="btn primary"
              type="submit"
              [disabled]="account.busy()"
              appTip="{{
                mode() === 'register'
                  ? 'Create the account and encrypt this device\\'s data for sync.'
                  : 'Sign in and unlock your synced data on this device.'
              }}"
            >
              {{
                account.busy() ? 'Working…' : mode() === 'register' ? 'Create account' : 'Sign in'
              }}
            </button>
          </div>
        } @else {
          <div class="update-summary">
            <div>
              <span class="muted">Signed in as</span><strong>{{ account.session()?.email }}</strong>
            </div>
            <div>
              <span class="muted">Membership</span><strong>{{ planLabel() }}</strong>
            </div>
            <div>
              <span class="muted">Active tasks</span
              ><span id="account-task-usage">{{ tasks.usageLabel() }}</span>
            </div>
            <div>
              <span class="muted">Sync</span><span>{{ syncLabel() }}</span>
            </div>
            <div>
              <span class="muted">Devices</span
              ><span>{{ account.me()?.devices?.length ?? '–' }}</span>
            </div>
          </div>
          @if (sync.error()) {
            <div class="callout error" role="alert">
              <p>{{ sync.error() }}</p>
            </div>
          }

          @if (!account.canSync()) {
            <div class="callout" id="upgrade-callout">
              <p>
                {{ membership.strategy().upgradeHint() ?? 'Sync needs a Pro plan.' }}
                {{
                  ent()?.trial_available
                    ? 'Start a free 14-day trial — no card needed.'
                    : 'Subscribe to turn Pro back on.'
                }}
              </p>
              <div class="dialog-actions">
                @if (ent()?.trial_available) {
                  <button
                    class="btn primary"
                    type="button"
                    (click)="trial()"
                    appTip="Start a 14-day Pro trial for encrypted sync between your devices."
                  >
                    Start free trial
                  </button>
                }
                <button
                  class="btn"
                  type="button"
                  (click)="checkout('pro_yearly')"
                  appTip="Subscribe to Pro (yearly). Payment is handled by the billing provider; we never see your card."
                >
                  Subscribe
                </button>
              </div>
            </div>
          } @else {
            <div class="dialog-actions">
              <button
                class="btn"
                type="button"
                (click)="sync.syncNow()"
                [disabled]="sync.state() === 'syncing'"
                appTip="Push local changes and fetch changes from your other devices now."
              >
                Sync now
              </button>
              <button
                class="btn"
                type="button"
                (click)="resetFromServer()"
                appTip="Discard this device's local copy and rebuild it from the server's encrypted master copy. Use this if the data on this device looks wrong."
              >
                Reset from server
              </button>
            </div>
            <label class="checkbox" for="acc-vault">
              <input
                type="checkbox"
                id="acc-vault"
                [checked]="account.vaultEnabled()"
                (change)="setVault($any($event.target).checked)"
                appTip="Keep an encrypted copy of your full data on the server so a new device can restore everything. Off = the server only relays changes between devices for up to 30 days."
              />
              <span>Keep an encrypted backup on the server (vault)</span>
            </label>
          }

          @if (account.me()?.devices; as devices) {
            <div class="field">
              <span class="muted small">Devices</span>
              <ul class="device-list">
                @for (d of devices; track d.id) {
                  <li>
                    <span
                      >{{ d.platform }} · v{{ d.client_version || '?' }} · seen
                      {{ rel(d.last_seen_at) }}{{ d.is_current ? ' (this device)' : '' }}</span
                    >
                    @if (!d.is_current) {
                      <button
                        class="btn ghost"
                        type="button"
                        (click)="revoke(d.id)"
                        appTip="Sign this device out. It keeps its local data but stops syncing."
                      >
                        Sign out
                      </button>
                    }
                  </li>
                }
              </ul>
            </div>
          }
          <div class="dialog-actions">
            <button
              class="btn danger"
              type="button"
              (click)="deleteAccount()"
              appTip="Delete your account on the server. Data on your devices is kept."
            >
              Delete account
            </button>
            <span class="spacer"></span>
            <button
              class="btn"
              type="button"
              (click)="dialog().close()"
              appTip="Close this window."
            >
              Close
            </button>
            <button
              class="btn"
              type="button"
              (click)="logout()"
              appTip="Log out on this device. Your data stays on this device."
            >
              Log out
            </button>
          </div>
        }
      </form>
    </dialog>
  `,
})
export class AccountDialog {
  readonly account = inject(AccountService);
  readonly membership = inject(MembershipService);
  readonly tasks = inject(TasksService);
  readonly sync = inject(SyncService);
  private readonly toast = inject(ToastService);
  readonly dialog = viewChild.required(DialogDirective);
  readonly mode = signal<'login' | 'register'>('register');
  readonly error = signal<string | null>(null);
  email = '';
  password = '';

  readonly ent = computed(() => this.account.entitlements());
  readonly planLabel = computed(() => {
    const e = this.ent();
    if (!e) return this.membership.label();
    const status =
      e.status === 'trialing' ? ' (trial)' : e.status === 'past_due' ? ' (payment due)' : '';
    const plan = e.plan === 'lifetime' ? ' · Lifetime' : '';
    return `${this.membership.label()}${plan}${status}`;
  });
  readonly freeLimitLabel = computed(() => {
    const limit = this.membership.strategy().maxActiveTasks;
    return limit === null ? 'Unlimited active tasks' : `Up to ${limit} active tasks`;
  });
  readonly syncLabel = computed(() => {
    const s = this.sync.state();
    if (s === 'off') return 'off';
    if (s === 'syncing') return 'syncing…';
    if (s === 'error') return 'error';
    if (s === 'update_required') return 'update required';
    const at = this.sync.lastSyncAt();
    return `${this.sync.pending() ? `${this.sync.pending()} pending · ` : ''}${at ? 'synced ' + relative(at) : 'not yet'}`;
  });

  rel(iso: string) {
    return relative(new Date(iso));
  }

  /** `mode` picks the form for anonymous users (header buttons); signed-in users see their account. */
  open(mode?: 'login' | 'register') {
    this.error.set(null);
    if (mode) this.mode.set(mode);
    this.dialog().open();
    if (this.account.signedIn()) void this.account.refreshMe();
  }

  async submit() {
    this.error.set(null);
    try {
      if (this.mode() === 'register') await this.account.register(this.email.trim(), this.password);
      else await this.account.login(this.email.trim(), this.password);
      this.password = '';
      this.toast.show(this.mode() === 'register' ? 'Account created' : 'Signed in');
      // Existing local data is adopted by the first sync run (SyncService), not here.
    } catch (e) {
      this.error.set(e instanceof Error ? e.message : String(e));
    }
  }

  async trial() {
    try {
      await this.account.startTrial();
      this.toast.show('Pro trial started — sync is on');
    } catch (e) {
      this.toast.error(e instanceof ApiError ? e.message : String(e));
    }
  }

  async checkout(plan: 'pro_monthly' | 'pro_yearly' | 'lifetime') {
    try {
      location.href = await this.account.checkout(plan);
    } catch (e) {
      this.toast.error(e instanceof Error ? e.message : String(e));
    }
  }

  async setVault(on: boolean) {
    try {
      await this.account.setVault(on);
      this.toast.show(on ? 'Encrypted backup on' : 'Encrypted backup off');
    } catch (e) {
      this.toast.error(e instanceof Error ? e.message : String(e));
    }
  }

  async resetFromServer() {
    if (!confirm('Replace the data on this device with the copy from the server?')) return;
    await this.sync.resetFromServer();
    this.toast.show(this.sync.error() ?? 'Rebuilt from the server');
  }

  async revoke(id: string) {
    await this.account.revokeDevice(id);
    this.toast.show('Device signed out');
  }

  async logout() {
    await this.account.logout();
    this.toast.show('Logged out — your data stays on this device');
  }

  async deleteAccount() {
    if (!confirm('Delete your account on the server? Data on your devices is kept.')) return;
    await this.account.deleteAccount();
    this.toast.show('Account deleted');
  }
}
