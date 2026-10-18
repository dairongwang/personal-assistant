import {
  ANONYMOUS,
  AdminMembership,
  AnonymousMembership,
  FREE_MAX_ACTIVE_TASKS,
  FreeMembership,
  PaidMembership,
  createMembership,
  type MembershipStrategy,
} from './membership';

describe('Membership strategies', () => {
  const all: MembershipStrategy[] = [
    new AnonymousMembership(),
    new FreeMembership(),
    new PaidMembership(),
    new AdminMembership(),
  ];

  it('every strategy answers the same questions', () => {
    for (const s of all) {
      expect(s.label).toBeTruthy();
      expect(s.checkActivateTask(0).allowed).toBe(true);
      expect(typeof s.usageLabel(1)).toBe('string');
      expect(s.canUse('sync')).toBe(s.features.includes('sync'));
    }
  });

  it('free memberships (with or without account) allow 3 active tasks', () => {
    for (const s of [new AnonymousMembership(), new FreeMembership()]) {
      expect(s.maxActiveTasks).toBe(FREE_MAX_ACTIVE_TASKS);
      expect(s.checkActivateTask(2)).toMatchObject({ allowed: true, limit: 3, remaining: 1 });
      const blocked = s.checkActivateTask(3);
      expect(blocked.allowed).toBe(false);
      expect(blocked.remaining).toBe(0);
      expect(blocked.reason).toMatch(/up to 3 active tasks/);
      expect(blocked.reason).toMatch(/upgrade to Pro/);
      expect(s.checkActivateTask(10).allowed).toBe(false);
      expect(s.usageLabel(2)).toBe('2 of 3 active tasks');
      expect(s.canUse('sync')).toBe(false);
      expect(s.canUpgrade).toBe(true);
      expect(s.upgradeHint()).toMatch(/Pro/);
    }
    expect(new AnonymousMembership().hasAccount).toBe(false);
    expect(new FreeMembership().hasAccount).toBe(true);
  });

  it('paid and admin memberships are unlimited and have the full feature set', () => {
    for (const s of [new PaidMembership(), new AdminMembership()]) {
      expect(s.maxActiveTasks).toBeNull();
      expect(s.checkActivateTask(6)).toMatchObject({ allowed: true, limit: null });
      expect(s.checkActivateTask(1000).remaining).toBe(Infinity);
      expect(s.usageLabel(12)).toBe('12 active tasks');
      expect(s.usageLabel(1)).toBe('1 active task');
      expect(s.canUse('sync')).toBe(true);
      expect(s.canUse('vault')).toBe(true);
      expect(s.canUpgrade).toBe(false);
      expect(s.upgradeHint()).toBeNull();
    }
    expect(new PaidMembership().canUse('admin')).toBe(false);
    expect(new AdminMembership().canUse('admin')).toBe(true);
    expect(new AdminMembership().label).toBe('Administrator');
  });

  it('createMembership takes the server values as authoritative', () => {
    const s = createMembership('free', { maxActiveTasks: 5, features: ['sync'] });
    expect(s.type).toBe('free');
    expect(s.maxActiveTasks).toBe(5);
    expect(s.checkActivateTask(4).allowed).toBe(true);
    expect(s.checkActivateTask(5).allowed).toBe(false);
    expect(s.canUse('sync')).toBe(true);
    const unlimitedFree = createMembership('free', { maxActiveTasks: null });
    expect(unlimitedFree.checkActivateTask(99).allowed).toBe(true);
  });

  it('unknown membership types (newer server) fall back to the safe free defaults', () => {
    const s = createMembership('platinum');
    expect(s.type).toBe('free');
    expect(s.maxActiveTasks).toBe(FREE_MAX_ACTIVE_TASKS);
    expect(createMembership(undefined).type).toBe('free');
    expect(ANONYMOUS.type).toBe('anonymous');
  });
});
