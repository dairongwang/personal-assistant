import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { AccountService } from '../account/account.service';
import { ApiError, ApiService } from '../account/api.service';
import { StoreService, type Snapshot } from '../data/store.service';
import { DbService } from '../db/db.service';
import { SCHEMA_VERSION, UnsupportedSchemaError } from '../db/migrations';
import type { Op } from '../db/models';
import { decryptJson, encryptJson } from './crypto';

interface PushResponse {
  accepted: { op_id: string; seq: number }[];
  last_seq: number;
}
interface PullResponse {
  ops: {
    seq: number;
    op_id: string;
    device_id: string;
    schema_version: number;
    ciphertext: string;
  }[];
  last_seq: number;
  cursor: number;
  has_more: boolean;
  snapshot_seq: number | null;
}
interface SnapshotOut {
  seq: number;
  schema_version: number;
  ciphertext: string;
}

export type SyncState = 'off' | 'idle' | 'syncing' | 'error' | 'update_required';

/**
 * API-based synchronisation. The local database is a cache; the server's op sequence is the
 * master order. One sync run = push outbox → pull (apply in seq order, skipping our own) →
 * ack → (optionally) upload an encrypted snapshot so any device can rebuild from the server.
 */
@Injectable({ providedIn: 'root' })
export class SyncService {
  private readonly api = inject(ApiService);
  private readonly account = inject(AccountService);
  private readonly store = inject(StoreService);
  private readonly dbs = inject(DbService);

  readonly state = signal<SyncState>('off');
  readonly error = signal<string | null>(null);
  readonly lastSyncAt = signal<Date | null>(null);
  readonly pending = signal(0);
  readonly cursor = signal(0);
  readonly enabled = computed(() => this.account.canSync());

  private timer: ReturnType<typeof setTimeout> | null = null;
  private running: Promise<void> | null = null;
  private snapshotDirty = false;

  constructor() {
    effect(() => {
      this.store.outboxChanged();
      untracked(() => {
        void this.store.outboxSize().then((n) => this.pending.set(n));
        if (this.enabled()) this.schedule(1500);
      });
    });
    effect(() => {
      const on = this.enabled();
      untracked(() => {
        this.state.set(on ? 'idle' : 'off');
        if (on) this.schedule(200);
      });
    });
  }

  init(): void {
    window.addEventListener('online', () => this.enabled() && this.schedule(500));
    document.addEventListener(
      'visibilitychange',
      () => !document.hidden && this.enabled() && this.schedule(500),
    );
    setInterval(() => this.enabled() && this.schedule(0), 5 * 60 * 1000);
    void this.dbs.db.getMeta<number>('cursor').then((c) => this.cursor.set(c ?? 0));
  }

  schedule(ms: number): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.syncNow(), ms);
  }

  /**
   * First sync of an account on this device: whatever is already here belongs to the user and
   * is queued for upload (once — guarded by a meta flag so a retried first sync does not
   * duplicate the ops).
   */
  private async adoptLocalData(): Promise<void> {
    const flag = `adopted:${this.account.session()?.accountId}`;
    if (await this.dbs.db.getMeta<boolean>(flag)) return;
    const ops = await this.store.opsForAllLocalData();
    if (ops.length) await this.store.enqueue(ops);
    await this.dbs.db.setMeta(flag, true);
  }

  async syncNow(): Promise<void> {
    if (this.running) return this.running;
    this.running = this.run().finally(() => (this.running = null));
    return this.running;
  }

  private aad() {
    return `account:${this.account.session()?.accountId}`;
  }

  private async run(): Promise<void> {
    const key = this.account.dataKey();
    const session = this.account.session();
    if (!key || !session || !this.enabled() || !navigator.onLine) return;
    this.state.set('syncing');
    this.error.set(null);
    try {
      let cursor = (await this.dbs.db.getMeta<number>('cursor')) ?? 0;
      if (cursor === 0) {
        // First sync on this device: queue local data for upload, rebuild the cache from the
        // server's snapshot (server = master), then re-apply the queued local changes on top —
        // they get the highest seqs when pushed, so every device ends up with them.
        await this.adoptLocalData();
        cursor = await this.bootstrapFromSnapshot(key);
        await this.store.reapplyOutbox();
      }

      await this.push(key);
      const newCursor = await this.pull(key, cursor, session.deviceId);
      if (newCursor !== cursor) {
        cursor = newCursor;
        await this.dbs.db.setMeta('cursor', cursor);
        this.cursor.set(cursor);
      }
      await this.api.post('/sync/ack', { cursor });
      if (this.snapshotDirty && this.account.vaultEnabled()) await this.uploadSnapshot(key, cursor);
      this.lastSyncAt.set(new Date());
      await this.dbs.db.setMeta('lastSyncAt', this.lastSyncAt()!.toISOString());
      this.state.set('idle');
    } catch (e) {
      if (e instanceof UnsupportedSchemaError || (e instanceof ApiError && e.upgradeRequired)) {
        this.state.set('update_required');
        this.error.set(e.message);
      } else if (e instanceof ApiError && e.status === 402) {
        this.state.set('off');
        this.error.set(e.message);
        await this.account.refreshMe();
      } else {
        this.state.set('error');
        this.error.set(e instanceof Error ? e.message : String(e));
      }
    } finally {
      this.pending.set(await this.store.outboxSize());
    }
  }

  private async push(key: CryptoKey): Promise<void> {
    for (;;) {
      const batch = await this.store.pendingOps(200);
      if (!batch.length) return;
      const ops = await Promise.all(
        batch.map(async ({ op }) => ({
          op_id: op.opId,
          schema_version: op.schema,
          ciphertext: await encryptJson(op, key, this.aad()),
        })),
      );
      const res = await this.api.post<PushResponse>('/sync/push', { ops });
      const seqByOp = new Map(res.accepted.map((a) => [a.op_id, a.seq]));
      await this.store.markPushed(batch.map((b) => ({ ...b, seq: seqByOp.get(b.op.opId) ?? 0 })));
      this.snapshotDirty = true;
    }
  }

  private async pull(key: CryptoKey, cursor: number, ownDevice: string): Promise<number> {
    for (;;) {
      const page = await this.api.get<PullResponse>(`/sync/pull?since=${cursor}&limit=200`);
      if (!page.ops.length) return cursor;
      const foreign = page.ops.filter((o) => o.device_id !== ownDevice);
      if (foreign.some((o) => o.schema_version > SCHEMA_VERSION)) {
        throw new UnsupportedSchemaError(Math.max(...foreign.map((o) => o.schema_version)));
      }
      const decrypted = await Promise.all(
        foreign.map(async (o) => ({
          seq: o.seq,
          op: await decryptJson<Op>(o.ciphertext, key, this.aad()),
        })),
      );
      await this.store.applyRemote(decrypted);
      if (decrypted.length) this.snapshotDirty = true;
      cursor = page.ops[page.ops.length - 1].seq;
      if (!page.has_more) return cursor;
    }
  }

  private async bootstrapFromSnapshot(key: CryptoKey): Promise<number> {
    try {
      const snap = await this.api.get<SnapshotOut>('/sync/snapshot');
      if (snap.schema_version > SCHEMA_VERSION)
        throw new UnsupportedSchemaError(snap.schema_version);
      const data = await decryptJson<Snapshot>(snap.ciphertext, key, this.aad() + ':snapshot');
      await this.store.importSnapshot(data, snap.seq);
      await this.dbs.db.setMeta('cursor', snap.seq);
      this.cursor.set(snap.seq);
      return snap.seq;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return 0; // no snapshot yet: replay ops from 0
      throw e;
    }
  }

  private async uploadSnapshot(key: CryptoKey, seq: number): Promise<void> {
    if (await this.store.outboxSize()) return; // only when local == server
    const snapshot = await this.store.exportSnapshot();
    const ciphertext = await encryptJson(snapshot, key, this.aad() + ':snapshot');
    try {
      await this.api.put('/sync/snapshot', { seq, schema_version: SCHEMA_VERSION, ciphertext });
      this.snapshotDirty = false;
    } catch (e) {
      if (!(e instanceof ApiError && (e.status === 409 || e.status === 402))) throw e;
    }
  }

  /** "Reset from server": drop the local cache and rebuild it from the master copy. */
  async resetFromServer(): Promise<void> {
    await this.store.clearAll();
    await this.dbs.db.setMeta('cursor', 0);
    this.cursor.set(0);
    await this.syncNow();
  }
}
