/**
 * Membership types — the *strategy* pattern for "what may this user do in the app?".
 *
 * The UI and the data services never branch on plan names. They ask the current strategy:
 *
 *   membership.strategy().checkActivateTask(openCount)   → may one more task be opened?
 *   membership.strategy().canUse('sync')                  → feature gate
 *   membership.strategy().label / usageLabel(n) / upgradeHint()
 *
 * | Type      | Who                                  | Active tasks | Features                     |
 * |-----------|--------------------------------------|--------------|------------------------------|
 * | anonymous | no account — everything on device    | 3            | local app (sponsored panel)  |
 * | free      | account without a paid subscription  | 3            | + trial / subscribe          |
 * | paid      | Pro trial/active/grace, Lifetime     | unlimited    | + sync, vault, push, ad_free |
 * | admin     | administrator (production support)   | unlimited    | everything paid + admin panel|
 *
 * The server owns the same table (services/account/membership.py) and sends its values with
 * `/me` → entitlements (`membership`, `max_active_tasks`, `features`); when signed in those
 * override the defaults below, so a policy change on the server reaches every device without
 * an app update. Without an account (or offline) the defaults apply.
 *
 * "Active" task = status `todo` or `in_progress` (what the dashboard calls *open*). Limits apply
 * to what the user does on this device — finishing or deleting tasks frees slots; data that
 * arrives through sync is never refused.
 */

export type MembershipType = 'anonymous' | 'free' | 'paid' | 'admin';
export type Feature =
  'sync' | 'vault' | 'push' | 'calendar_feed' | 'insights_full' | 'ad_free' | 'admin';

/** Default limit for free memberships (server: FREE_MAX_ACTIVE_TASKS). */
export const FREE_MAX_ACTIVE_TASKS = 3;
/** `ad_free`: no sponsored-content panel (core/ads) — free memberships see it, Pro buys it away. */
export const PAID_FEATURES: readonly Feature[] = [
  'sync',
  'vault',
  'push',
  'calendar_feed',
  'insights_full',
  'ad_free',
];

export interface TaskLimitCheck {
  allowed: boolean;
  /** null = unlimited */
  limit: number | null;
  /** Slots left before the limit (Infinity when unlimited). */
  remaining: number;
  /** Human explanation when not allowed. */
  reason: string | null;
}

export interface MembershipStrategy {
  readonly type: MembershipType;
  readonly label: string;
  /** Compact label for the header badge. */
  readonly shortLabel: string;
  readonly hasAccount: boolean;
  /** null = unlimited */
  readonly maxActiveTasks: number | null;
  readonly features: readonly Feature[];
  /** Whether the UI should offer trial/subscribe. */
  readonly canUpgrade: boolean;

  canUse(feature: Feature): boolean;
  /** May a user with `activeCount` open tasks open one more? */
  checkActivateTask(activeCount: number): TaskLimitCheck;
  /** Short usage text for the header/task list, e.g. "2 of 3 active tasks". */
  usageLabel(activeCount: number): string;
  /** What to tell the user when they hit a limit / look at the plan (null = nothing to sell). */
  upgradeHint(): string | null;
}

export interface StrategyOverrides {
  maxActiveTasks?: number | null;
  features?: readonly string[];
}

abstract class BaseMembership implements MembershipStrategy {
  abstract readonly type: MembershipType;
  abstract readonly label: string;
  abstract readonly hasAccount: boolean;
  abstract readonly canUpgrade: boolean;
  readonly maxActiveTasks: number | null;
  readonly features: readonly Feature[];

  protected constructor(
    defaultLimit: number | null,
    defaultFeatures: readonly Feature[],
    overrides: StrategyOverrides = {},
  ) {
    this.maxActiveTasks =
      overrides.maxActiveTasks === undefined ? defaultLimit : overrides.maxActiveTasks;
    this.features = overrides.features
      ? (overrides.features.filter(Boolean) as Feature[])
      : defaultFeatures;
  }

  get shortLabel(): string {
    return this.label;
  }

  canUse(feature: Feature): boolean {
    return this.features.includes(feature);
  }

  checkActivateTask(activeCount: number): TaskLimitCheck {
    const limit = this.maxActiveTasks;
    if (limit === null) return { allowed: true, limit: null, remaining: Infinity, reason: null };
    const remaining = Math.max(0, limit - activeCount);
    const allowed = activeCount < limit;
    return {
      allowed,
      limit,
      remaining,
      reason: allowed ? null : this.limitReason(limit),
    };
  }

  usageLabel(activeCount: number): string {
    const limit = this.maxActiveTasks;
    if (limit === null) return `${activeCount} active task${activeCount === 1 ? '' : 's'}`;
    return `${activeCount} of ${limit} active tasks`;
  }

  abstract upgradeHint(): string | null;

  protected limitReason(limit: number): string {
    return `${this.label} membership: up to ${limit} active tasks. Finish or delete a task to add another${
      this.upgradeHint() ? ', or upgrade to Pro for unlimited tasks' : ''
    }.`;
  }
}

export class AnonymousMembership extends BaseMembership {
  readonly type = 'anonymous' as const;
  readonly label = 'Free';
  readonly hasAccount = false;
  readonly canUpgrade = true;
  constructor(overrides?: StrategyOverrides) {
    super(FREE_MAX_ACTIVE_TASKS, [], overrides);
  }
  upgradeHint(): string {
    return 'Create an account and go Pro for unlimited active tasks and encrypted sync between your devices.';
  }
}

export class FreeMembership extends BaseMembership {
  readonly type = 'free' as const;
  readonly label = 'Free';
  readonly hasAccount = true;
  readonly canUpgrade = true;
  constructor(overrides?: StrategyOverrides) {
    super(FREE_MAX_ACTIVE_TASKS, [], overrides);
  }
  upgradeHint(): string {
    return 'Go Pro for unlimited active tasks and encrypted sync between your devices.';
  }
}

export class PaidMembership extends BaseMembership {
  readonly type = 'paid' as const;
  readonly label = 'Pro';
  readonly hasAccount = true;
  readonly canUpgrade = false;
  constructor(overrides?: StrategyOverrides) {
    super(null, PAID_FEATURES, overrides);
  }
  upgradeHint(): null {
    return null;
  }
}

export class AdminMembership extends BaseMembership {
  readonly type = 'admin' as const;
  readonly label = 'Administrator';
  override get shortLabel(): string {
    return 'Admin';
  }
  readonly hasAccount = true;
  readonly canUpgrade = false;
  constructor(overrides?: StrategyOverrides) {
    super(null, [...PAID_FEATURES, 'admin'], overrides);
  }
  upgradeHint(): null {
    return null;
  }
}

const FACTORIES: Record<MembershipType, (o?: StrategyOverrides) => MembershipStrategy> = {
  anonymous: (o) => new AnonymousMembership(o),
  free: (o) => new FreeMembership(o),
  paid: (o) => new PaidMembership(o),
  admin: (o) => new AdminMembership(o),
};

export function isMembershipType(value: unknown): value is MembershipType {
  return typeof value === 'string' && value in FACTORIES;
}

/**
 * Build the strategy for a membership type. Unknown types (a newer server) fall back to
 * `free` — the safe default — unless the server explicitly says the tasks are unlimited.
 */
export function createMembership(
  type: string | null | undefined,
  overrides?: StrategyOverrides,
): MembershipStrategy {
  const key: MembershipType = isMembershipType(type) ? type : 'free';
  return FACTORIES[key](overrides);
}

export const ANONYMOUS: MembershipStrategy = new AnonymousMembership();
