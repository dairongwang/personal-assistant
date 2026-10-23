import { Component, computed, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { TasksService } from '../data/tasks.service';
import { MembershipService } from '../membership/membership.service';
import { SyncService } from '../sync/sync.service';
import { DialogDirective } from '../ui/dialog.directive';
import { TipDirective } from '../ui/tip.directive';
import { ToastService } from '../ui/toast.service';
import { relative } from '../update/update-policy';
import {
  AccountService,
  type PaymentMethod,
  type PaymentOut,
  type Plan,
  type PlanInfo,
} from './account.service';
import { ApiError } from './api.service';

/** Until the server's catalogue is loaded (prices come from its PRICE_* settings). */
const FALLBACK_PLANS: PlanInfo[] = [
  {
    id: 'pro_monthly',
    title: 'Pro — monthly',
    amount: '',
    amount_cents: 0,
    currency: 'USD',
    interval: 'month',
    period_days: 30,
    refund_rule: '',
  },
  {
    id: 'pro_yearly',
    title: 'Pro — yearly',
    amount: '',
    amount_cents: 0,
    currency: 'USD',
    interval: 'year',
    period_days: 365,
    refund_rule: '',
  },
  {
    id: 'lifetime',
    title: 'Lifetime',
    amount: '',
    amount_cents: 0,
    currency: 'USD',
    interval: null,
    period_days: null,
    refund_rule: '',
  },
];

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
              <span class="small"
                >+ encrypted sync & backup across devices · no sponsored content</span
              >
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
              <span class="muted">Billing</span
              ><strong id="account-billing">{{ billingLabel() }}</strong>
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

          @if (account.canSync()) {
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

          @if (hasPaidSubscription()) {
            <!-- A paid Pro subscription or Lifetime: renewal date, renew / cancel, the
                 provider's billing page. -->
            <div class="callout" id="subscription-callout">
              <p>{{ subscriptionHint() }}</p>
              @if (ent()?.last_payment; as last) {
                @if (last.status === 'failed') {
                  <p class="small" id="payment-failed" role="alert">
                    <strong>Payment failed:</strong> {{ money(last.amount, last.currency) }} for
                    {{ planTitle(last.plan) }} on {{ date(last.paid_at) }}
                    {{ last.failure_reason ? '— ' + last.failure_reason : '' }}
                  </p>
                }
              }
              <div class="dialog-actions">
                @if (ent()?.can_manage) {
                  <button
                    class="btn"
                    type="button"
                    id="btn-manage"
                    [disabled]="busy()"
                    (click)="manage()"
                    appTip="Opens your billing page at the payment provider: payment method, invoices, receipts."
                  >
                    {{
                      ent()?.status === 'past_due' ? 'Update payment method ↗' : 'Manage billing ↗'
                    }}
                  </button>
                }
                @if (ent()?.can_cancel) {
                  <button
                    class="btn danger"
                    type="button"
                    id="btn-cancel"
                    [disabled]="busy()"
                    (click)="cancel()"
                    appTip="{{
                      ent()?.plan === 'lifetime'
                        ? 'End your Lifetime membership now; ' + refundRule('lifetime')
                        : 'End the subscription now; the unused days of the current period are refunded.'
                    }}"
                  >
                    {{ ent()?.plan === 'lifetime' ? 'Cancel membership' : 'Cancel subscription' }}
                  </button>
                }
              </div>
            </div>
          }

          @if (canSubscribe()) {
            <!-- Free, trial, past-due and lapsed accounts: pick a plan and pay. Hidden while a
                 paid subscription renews normally, for Lifetime, and for administrators. -->
            <div class="callout" id="upgrade-callout">
              <p>{{ subscribeHint() }}</p>
              @if (ent()?.trial_available) {
                <div class="dialog-actions">
                  <button
                    class="btn primary"
                    type="button"
                    (click)="trial()"
                    appTip="Start a 14-day Pro trial for encrypted sync between your devices."
                  >
                    Start free trial
                  </button>
                </div>
              }
              <div class="field" id="checkout">
                <label for="acc-plan">{{ subscribeTitle() }}</label>
                <select
                  class="input"
                  id="acc-plan"
                  name="plan"
                  [ngModel]="plan()"
                  (ngModelChange)="plan.set($event)"
                  appTip="Monthly or yearly subscription, or a one-off Lifetime purchase."
                >
                  @for (p of plans(); track p.id) {
                    <option [value]="p.id">{{ planOption(p) }}</option>
                  }
                </select>
                <p class="muted small" id="refund-rule">{{ refundRule(plan()) }}</p>
                @if (methodsForPlan().length) {
                  <div class="dialog-actions payment-methods">
                    @for (m of methodsForPlan(); track m.name) {
                      <button
                        class="btn"
                        [class.primary]="m.available"
                        type="button"
                        [disabled]="paying()"
                        (click)="checkout(m)"
                        [attr.data-method]="m.name"
                        [attr.data-available]="m.available"
                        appTip="{{
                          m.available
                            ? m.description +
                              ' We never see your card or PayPal details.' +
                              (m.fake
                                ? ' Built-in fake: no credentials configured, nothing is charged.'
                                : '')
                            : m.label +
                              ' is not set up on this server yet — its credentials must be added on the server (docs/DEPLOYMENT.md → Payments).'
                        }}"
                      >
                        Pay with {{ m.label
                        }}{{ m.fake ? ' (fake)' : m.sandbox ? ' (test mode)' : '' }}
                      </button>
                    }
                  </div>
                } @else if (methods() === null) {
                  <p class="muted small">Loading payment methods…</p>
                } @else if (methods()?.length) {
                  <p class="muted small">
                    This plan is not available with the configured payment methods.
                  </p>
                } @else {
                  <p class="muted small">Payments are not available right now.</p>
                }
              </div>
            </div>
          }

          @if (history().length) {
            <div class="field" id="payment-history">
              <span class="muted small">Payments</span>
              <ul class="device-list">
                @for (p of history(); track p.id) {
                  <li>
                    <span
                      >{{ date(p.paid_at) }} · {{ planTitle(p.plan) }} ·
                      {{ money(p.amount, p.currency) }} · {{ paymentState(p) }}</span
                    >
                  </li>
                }
              </ul>
            </div>
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
  readonly plan = signal<Plan>('pro_yearly');
  /** Payment methods the server offers (null = not loaded yet). */
  readonly methods = signal<PaymentMethod[] | null>(null);
  /** The paid memberships with their prices (from the same catalogue call). */
  readonly plans = signal<PlanInfo[]>(FALLBACK_PLANS);
  /** This account's charges, failures and refunds (newest first). */
  readonly history = signal<PaymentOut[]>([]);
  readonly paying = signal(false);
  /** A cancel / manage call is in flight. */
  readonly busy = signal(false);

  readonly ent = computed(() => this.account.entitlements());
  /** Pay buttons for the selected plan. Configured methods only (a provider without a price
   *  for the plan is skipped); while *nothing* is configured yet every method is shown so the
   *  buttons exist and a click explains what the server is missing. */
  readonly methodsForPlan = computed(() => {
    const all = this.methods() ?? [];
    const configured = all.filter((m) => m.available);
    return (configured.length ? configured : all).filter((m) => m.plans.includes(this.plan()));
  });
  /** A paid Pro subscription or Lifetime that is in force (trial excluded; admins need none). */
  readonly hasPaidSubscription = computed(() => {
    const e = this.ent();
    return (
      !!e &&
      e.membership !== 'admin' &&
      (e.plan === 'pro' || e.plan === 'lifetime') &&
      e.status !== 'trialing'
    );
  });
  /** Anyone who could (still) buy Pro: free, trial and lapsed accounts, a failed renewal
   *  (subscribe again with another method), a subscription the provider ended that is still
   *  running on the paid period. Not admins, not a subscription that renews normally, not
   *  Lifetime. */
  readonly canSubscribe = computed(() => {
    const e = this.ent();
    if (!e || e.membership === 'admin' || e.plan === 'lifetime') return false;
    if (e.plan === 'free' || e.status === 'trialing' || e.status === 'past_due') return true;
    return e.cancel_at_period_end;
  });
  readonly subscribeTitle = computed(() => {
    const e = this.ent();
    if (e?.status === 'trialing') return 'Keep Pro after the trial';
    if (e?.status === 'past_due') return 'Or subscribe again';
    if (e?.cancel_at_period_end) return 'Subscribe again';
    return 'Subscribe to Pro';
  });
  /** Pro that was never charged yet: our free trial, or a subscription whose first charge is
   *  still ahead (the trial carried over to the provider — PayPal reports that as active). */
  readonly onTrial = computed(() => {
    const e = this.ent();
    return (
      !!e &&
      e.plan === 'pro' &&
      !e.last_payment &&
      (e.status === 'trialing' || e.status === 'active')
    );
  });
  /** The summary row: when the paid membership ends / renews. */
  readonly billingLabel = computed(() => {
    const e = this.ent();
    if (!e) return '–';
    if (e.membership === 'admin') return 'Not needed (administrator)';
    const until = e.access_until ? this.date(e.access_until) : null;
    if (e.plan === 'lifetime') return 'Lifetime — never expires';
    if (this.onTrial()) return until ? `Trial ends ${until}` : 'Trial';
    if (e.status === 'past_due')
      return until ? `Payment failed · Pro until ${until}` : 'Payment failed';
    if (e.plan === 'pro' && e.cancel_at_period_end) return until ? `Ends ${until}` : 'Ends soon';
    if (e.plan === 'pro') return until ? `Renews ${until}` : 'Active';
    if (e.status === 'canceled') return 'Canceled';
    return 'No subscription';
  });
  readonly subscriptionHint = computed(() => {
    const e = this.ent();
    if (!e) return '';
    const until = e.access_until ? this.date(e.access_until) : 'the end of the period';
    if (e.plan === 'lifetime')
      return `Lifetime membership — paid once, never expires. ${this.refundRule('lifetime')}`;
    if (e.status === 'past_due')
      return `Pro stays on until ${until} while the payment is retried. Update your payment method at the provider, or subscribe again below.`;
    if (e.cancel_at_period_end)
      return `The provider ended the renewal — Pro runs until ${until}. Subscribe again below to continue afterwards.`;
    if (this.onTrial())
      return `Your subscription starts when the free trial ends (${until}). Cancel before that and nothing is charged.`;
    return `Pro renews automatically on ${until}. Cancel any time — the unused days of the current period are refunded.`;
  });
  readonly subscribeHint = computed(() => {
    const e = this.ent();
    if (e?.status === 'past_due')
      return 'Pick a plan to start a new subscription with another payment method.';
    if (e?.cancel_at_period_end)
      return 'Pick a plan to continue with Pro after the current period.';
    if (e?.status === 'trialing') {
      const end = e.current_period_end ? new Date(e.current_period_end) : null;
      const until = end ? ` until ${this.date(e.current_period_end!)}` : '';
      // The server carries the trial over to the provider (paid billing starts when it ends)
      // unless fewer than 48 h remain — Stripe cannot defer a first charge that close.
      const soon = !end || end.getTime() - Date.now() < 48 * 3600 * 1000;
      return `Your free Pro trial runs${until}. Subscribe now to keep unlimited tasks and encrypted sync — ${
        soon ? 'billing starts right away.' : 'paid billing only starts when the trial ends.'
      }`;
    }
    const hint = this.membership.strategy().upgradeHint() ?? 'Sync needs a Pro plan.';
    return `${hint} ${e?.trial_available ? 'Start a free 14-day trial — no card needed — or subscribe right away.' : 'Subscribe to turn Pro back on.'}`;
  });
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

  planTitle(plan: string) {
    return this.plans().find((p) => p.id === plan)?.title ?? plan;
  }

  /** "Pro — monthly · $1.00 / month" */
  planOption(p: PlanInfo) {
    if (!p.amount) return p.title;
    const price = this.money(p.amount, p.currency);
    return `${p.title} · ${price}${p.interval ? ' / ' + p.interval : ' once'}`;
  }

  refundRule(plan: string) {
    return this.plans().find((p) => p.id === plan)?.refund_rule ?? '';
  }

  money(amount: string, currency: string) {
    try {
      return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(
        Number(amount),
      );
    } catch {
      return `${amount} ${currency}`;
    }
  }

  paymentState(p: PaymentOut) {
    if (p.status === 'failed') return `failed${p.failure_reason ? ' — ' + p.failure_reason : ''}`;
    if (p.status === 'refunded') return `refunded ${this.money(p.refunded, p.currency)}`;
    if (p.status === 'partially_refunded')
      return `paid · ${this.money(p.refunded, p.currency)} refunded`;
    return 'paid';
  }

  date(iso: string) {
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  }

  /** `mode` picks the form for anonymous users (header buttons); signed-in users see their account. */
  open(mode?: 'login' | 'register') {
    this.error.set(null);
    if (mode) this.mode.set(mode);
    this.dialog().open();
    if (this.account.signedIn()) {
      void this.account.refreshMe();
      void this.loadMethods();
    }
  }

  async loadMethods() {
    try {
      const catalog = await this.account.paymentMethods();
      this.methods.set(catalog.methods);
      if (catalog.plans?.length) this.plans.set(catalog.plans);
    } catch {
      this.methods.set([]); // offline or billing not configured: no buttons, no error
    }
    try {
      this.history.set(await this.account.payments());
    } catch {
      this.history.set([]);
    }
  }

  async submit() {
    this.error.set(null);
    try {
      if (this.mode() === 'register') await this.account.register(this.email.trim(), this.password);
      else await this.account.login(this.email.trim(), this.password);
      this.password = '';
      this.toast.show(this.mode() === 'register' ? 'Account created' : 'Signed in');
      void this.loadMethods(); // the dialog stays open and now shows the account view
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

  /** Sends the browser to the provider's hosted checkout for the selected plan. An
   *  unconfigured method answers 503 with what is missing — shown as a toast. */
  async checkout(method: PaymentMethod) {
    this.paying.set(true);
    try {
      location.href = await this.account.checkout(this.plan(), method.name);
    } catch (e) {
      this.toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      this.paying.set(false);
    }
  }

  /** Ends the membership now. The server says first what comes back (prorated unused days
   *  of the period, or the flat Lifetime amount) — that is what the confirmation shows. */
  async cancel() {
    const lifetime = this.ent()?.plan === 'lifetime';
    this.busy.set(true);
    try {
      const preview = await this.account.cancelPreview();
      const refund =
        preview.amount_cents > 0
          ? `${this.money(preview.amount, preview.currency)} will be refunded (${preview.note})`
          : `Nothing is refunded (${preview.note})`;
      const question = lifetime
        ? `End your Lifetime membership now? ${refund}. You go back to the Free limits immediately.`
        : `End your Pro subscription now? ${refund}. Pro stops immediately.`;
      if (!confirm(question)) return;
      const result = await this.account.cancelSubscription();
      const back = Number(result.refund.amount_cents) > 0;
      this.toast.show(
        back
          ? `${lifetime ? 'Membership' : 'Subscription'} ended — ${this.money(result.refund.amount, result.refund.currency)} refunded`
          : `${lifetime ? 'Membership' : 'Subscription'} ended`,
      );
      this.history.set(await this.account.payments());
    } catch (e) {
      this.toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      this.busy.set(false);
    }
  }

  /** Sends the browser to the provider's billing page; it returns to /?billing=managed. */
  async manage() {
    this.busy.set(true);
    try {
      location.href = await this.account.manageUrl();
    } catch (e) {
      this.toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      this.busy.set(false);
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
