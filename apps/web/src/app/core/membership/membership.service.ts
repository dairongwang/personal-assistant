import { Injectable, computed, inject } from '@angular/core';
import { AccountService } from '../account/account.service';
import { ANONYMOUS, type MembershipStrategy, createMembership } from './membership';

/**
 * Picks the membership strategy for the current state of the device:
 *
 *   not signed in                      → AnonymousMembership
 *   signed in, `/me` loaded            → what the server resolved (role → admin; billing → paid/free),
 *                                        with the server's limits and features
 *   signed in, offline / not loaded    → the last membership this device saw (cached with the session)
 *
 * Everything else in the app consults `strategy()`; nobody else knows plan names.
 */
@Injectable({ providedIn: 'root' })
export class MembershipService {
  private readonly account = inject(AccountService);

  readonly strategy = computed<MembershipStrategy>(() => {
    if (!this.account.signedIn()) return ANONYMOUS;
    const ent = this.account.entitlements();
    if (ent) {
      return createMembership(ent.membership, {
        maxActiveTasks: ent.max_active_tasks,
        features: ent.features,
      });
    }
    const cached = this.account.session();
    return createMembership(cached?.membership, {
      maxActiveTasks: cached?.maxActiveTasks,
    });
  });

  readonly type = computed(() => this.strategy().type);
  readonly label = computed(() => this.strategy().label);
  readonly isAdmin = computed(() => this.strategy().canUse('admin'));
  readonly canUpgrade = computed(() => this.strategy().canUpgrade);
}
