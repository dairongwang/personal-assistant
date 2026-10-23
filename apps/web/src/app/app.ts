import {
  AfterViewInit,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
  viewChild,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { AccountDialog } from './core/account/account-dialog';
import { AccountService } from './core/account/account.service';
import { AdInfoDialog } from './core/ads/ad-info-dialog';
import { AdPanel } from './core/ads/ad-panel';
import { AdsService } from './core/ads/ads.service';
import { AdminDialog } from './core/admin/admin-dialog';
import { EventsService } from './core/data/events.service';
import { TasksService } from './core/data/tasks.service';
import { TodayService } from './core/data/today.service';
import type { CalendarEvent, Task, TaskStatus } from './core/db/models';
import { MembershipService } from './core/membership/membership.service';
import { RELEASE } from './core/release-info';
import { SyncService } from './core/sync/sync.service';
import { formatDue, formatEventTime } from './core/ui/format';
import { TipDirective } from './core/ui/tip.directive';
import { ToastService } from './core/ui/toast.service';
import { TooltipService, topLayerHide, topLayerShow } from './core/ui/tooltip.service';
import { UiStateService, isTyping } from './core/ui/ui-state.service';
import { ForcedUpdate } from './core/update/forced-update';
import { UpdateDialog } from './core/update/update-dialog';
import { UpdateService } from './core/update/update.service';
import { EventDialog } from './features/dashboard/event-dialog';
import { TaskDialog } from './features/dashboard/task-dialog';

@Component({
  selector: 'app-root',
  imports: [
    FormsModule,
    TipDirective,
    TaskDialog,
    EventDialog,
    UpdateDialog,
    AccountDialog,
    AdminDialog,
    AdPanel,
    AdInfoDialog,
    ForcedUpdate,
  ],
  templateUrl: './app.html',
})
export class App implements AfterViewInit {
  readonly tasks = inject(TasksService);
  readonly events = inject(EventsService);
  readonly today = inject(TodayService);
  readonly account = inject(AccountService);
  readonly membership = inject(MembershipService);
  readonly sync = inject(SyncService);
  readonly update = inject(UpdateService);
  readonly ads = inject(AdsService);
  readonly toast = inject(ToastService);
  readonly tooltips = inject(TooltipService);
  private readonly ui = inject(UiStateService);

  readonly version = RELEASE.version;
  readonly online = signal(navigator.onLine);
  readonly todayLabel = new Date().toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  });
  readonly formatDue = formatDue;
  readonly formatEventTime = formatEventTime;

  readonly taskDialog = viewChild.required(TaskDialog);
  readonly eventDialog = viewChild.required(EventDialog);
  readonly updateDialog = viewChild.required(UpdateDialog);
  readonly accountDialog = viewChild.required(AccountDialog);
  readonly adminDialog = viewChild.required(AdminDialog);
  readonly adInfoDialog = viewChild.required(AdInfoDialog);
  readonly tooltipHost = viewChild.required<ElementRef<HTMLElement>>('tooltipHost');
  readonly toastHost = viewChild.required<ElementRef<HTMLElement>>('toastHost');

  readonly emptyText = computed(() =>
    this.tasks.search() || this.tasks.filterStatus()
      ? 'No tasks match.'
      : 'No tasks yet. Tap + to add one.',
  );
  /** Header badge: membership + task usage, e.g. "Free · 2/3" or "Pro". */
  readonly membershipBadge = computed(() => {
    const check = this.tasks.limit();
    const label = this.membership.strategy().shortLabel;
    return check.limit === null ? label : `${label} · ${this.tasks.activeCount()}/${check.limit}`;
  });
  readonly membershipTip = computed(() => {
    const s = this.membership.strategy();
    const usage = this.tasks.usageLabel();
    const who = s.hasAccount
      ? `Signed in as ${this.account.session()?.email ?? ''}. `
      : 'No account. ';
    const hint = s.upgradeHint();
    return `${who}${s.label} membership: ${usage}${s.maxActiveTasks === null ? ' (unlimited)' : ''}.${hint ? ' ' + hint : ''} Tap for account options.`;
  });
  readonly accountTip = computed(() => {
    if (!this.account.signedIn()) return 'Account: log in or register.';
    const s = this.sync.state();
    const who = this.account.session()?.email ?? '';
    if (s === 'syncing') return `Signed in as ${who}. Syncing…`;
    if (s === 'error')
      return `Signed in as ${who}. Sync problem: ${this.sync.error()} Tap for details.`;
    if (s === 'update_required')
      return `Signed in as ${who}. Sync is paused until the app is updated.`;
    if (s === 'off') return `Signed in as ${who}. Sync is off (needs Pro). Tap to start a trial.`;
    return `Signed in as ${who}. ${this.sync.pending() ? `${this.sync.pending()} changes waiting to sync.` : 'Everything is synced.'} Tap for account and devices.`;
  });
  readonly updateBannerVisible = computed(
    () =>
      this.update.decision().ready && !this.update.decision().required && !this.update.autoUpdate(),
  );

  constructor() {
    // Toast: show/hide in the top layer (above dialogs).
    effect(() => {
      const t = this.toast.current();
      const host = this.toastHost().nativeElement;
      if (t) {
        topLayerShow(host);
        host.classList.remove('show');
        requestAnimationFrame(() => host.classList.add('show'));
      } else {
        host.classList.remove('show');
        setTimeout(() => !this.toast.current() && topLayerHide(host), 250);
      }
    });
    effect(() => {
      const host = this.tooltipHost().nativeElement;
      host.hidden = !this.tooltips.visible();
    });
  }

  ngAfterViewInit(): void {
    void this.init();
  }

  private async init() {
    this.tooltips.attach(this.tooltipHost().nativeElement);
    this.tooltips.maybeShowFirstRunHint();
    window.addEventListener('online', () => this.online.set(true));
    window.addEventListener('offline', () => this.online.set(false));
    document.addEventListener('focusin', () => this.ui.poke());
    document.addEventListener('focusout', () => setTimeout(() => this.ui.poke(), 0));
    document.addEventListener('visibilitychange', () => this.ui.poke());
    document.addEventListener('keydown', (e) => this.shortcuts(e));

    await this.account.init();
    this.sync.init();
    this.update.init();
    this.ads.init();

    const params = new URLSearchParams(location.search);
    if (params.get('new') === 'task') {
      history.replaceState(null, '', '/');
      setTimeout(() => this.taskDialog().open(), 50);
    }
    if (params.get('billing') === 'canceled') {
      history.replaceState(null, '', '/');
      this.toast.show('Checkout canceled — nothing was charged.');
    }
    if (params.get('billing') === 'managed') {
      history.replaceState(null, '', '/');
      await this.account.refreshMe();
      this.toast.show('Billing details refreshed.');
    }
    if (params.get('billing') === 'done') {
      history.replaceState(null, '', '/');
      await this.account.refreshMe();
      this.toast.show(
        this.account.canSync()
          ? 'Thanks! Pro is active — sync is on.'
          : 'Payment received; entitlements refresh shortly.',
      );
    }
  }

  private shortcuts(e: KeyboardEvent) {
    if (isTyping() || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('dialog[open]'))
      return;
    if (e.key === 'n') {
      e.preventDefault();
      this.taskDialog().open();
    } else if (e.key === 'e') {
      e.preventDefault();
      this.eventDialog().open();
    } else if (e.key === '/') {
      e.preventDefault();
      document.getElementById('search')?.focus();
    }
  }

  checkLabel(task: Task) {
    return task.status === 'done'
      ? `Mark "${task.title}" as not done`
      : `Mark "${task.title}" as done`;
  }

  onFilter(value: string) {
    this.tasks.filterStatus.set(value as TaskStatus | '');
  }

  async toggle(task: Task) {
    try {
      await this.tasks.toggleDone(task);
    } catch (e) {
      this.toast.error(e instanceof Error ? e.message : String(e));
    }
  }

  openTask(task: Task | null = null) {
    this.taskDialog().open(task);
  }
  openEvent(ev: CalendarEvent | null = null) {
    this.eventDialog().open(ev);
  }
  openAdmin() {
    this.adminDialog().open();
  }
  openAdInfo() {
    this.adInfoDialog().open();
  }
  openUpdate() {
    this.updateDialog().open();
  }
  openAccount(mode?: 'login' | 'register') {
    this.accountDialog().open(mode);
  }
  async logout() {
    const who = this.account.session()?.email ?? '';
    await this.account.logout();
    this.toast.show(`Logged out${who ? ' of ' + who : ''} — your data stays on this device`);
  }
  toggleHelp() {
    this.tooltips.setHelpMode(!this.tooltips.helpMode());
  }

  setAutoUpdate(on: boolean) {
    this.update.setAutoUpdate(on);
    this.toast.show(
      on
        ? 'New versions will be installed automatically'
        : 'Automatic updates are off — use the Update button to install them',
    );
  }
}
