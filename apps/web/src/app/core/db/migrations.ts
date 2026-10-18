/**
 * Version-controlled migrations — the single place where the local data format changes.
 *
 * Two things must evolve together and are kept in one table here:
 *   1. the IndexedDB schema (Dexie `version(n).stores().upgrade()`), applied on the device when
 *      a newer app opens an older database — automatically, before the UI renders;
 *   2. op "upcasters": sync ops carry the schema version they were written with. When a device
 *      receives an op written by an older app version it is upcast step by step to the current
 *      format before being applied. Ops written by a *newer* schema than this app understands are
 *      never applied — the app asks for an update instead (see SyncService / UpdateService).
 *
 * Rules: never edit or remove a step; add a new one. Bump `SCHEMA_VERSION` and
 * `release.json → schemaVersion` together (a unit test enforces they match).
 */
import type Dexie from 'dexie';
import type { Transaction } from 'dexie';

export const SCHEMA_VERSION = 2;

interface MigrationStep {
  version: number;
  /** Dexie store definitions for this version (only changed tables need listing). */
  stores: Record<string, string | null>;
  /** Data transformation for rows that already exist on the device. */
  upgrade?: (tx: Transaction) => Promise<void> | void;
  /** Transform an op written with schema `version - 1` into schema `version`. */
  upcastOp?: (op: AnyOp) => AnyOp;
}

// Loosely typed on purpose: migrations deal with historical shapes.
export type AnyOp = {
  schema: number;
  entity: string;
  kind: string;
  data: Record<string, unknown> | null;
} & Record<string, unknown>;

export const MIGRATIONS: MigrationStep[] = [
  {
    // v1 — first local-first release: tasks, events, outbox, meta.
    version: 1,
    stores: {
      tasks: 'id, status, dueAt, updatedAt',
      events: 'id, startAt',
      outbox: '++localSeq, op.opId',
      meta: 'key',
    },
  },
  {
    // v2 — tags become indexed (multi-entry) and tasks gain completedAt. Existing rows get
    // defaults; ops from v1 devices are upcast the same way.
    version: 2,
    stores: {
      tasks: 'id, status, dueAt, updatedAt, *tags',
    },
    upgrade: async (tx) => {
      await tx
        .table('tasks')
        .toCollection()
        .modify((t: Record<string, unknown>) => {
          t['tags'] ??= [];
          t['completedAt'] ??= t['status'] === 'done' ? (t['updatedAt'] ?? null) : null;
        });
    },
    upcastOp: (op) => {
      if (op.entity === 'task' && op.data) {
        op.data['tags'] ??= [];
        op.data['completedAt'] ??=
          op.data['status'] === 'done' ? (op.data['updatedAt'] ?? null) : null;
      }
      return { ...op, schema: 2 };
    },
  },
];

/** Register every migration step on a Dexie instance (in order). */
export function applyMigrations(db: Dexie): void {
  for (const step of MIGRATIONS) {
    const v = db.version(step.version).stores(step.stores);
    if (step.upgrade) v.upgrade(step.upgrade);
  }
}

export class UnsupportedSchemaError extends Error {
  constructor(public readonly opSchema: number) {
    super(
      `Data was written by a newer app version (schema ${opSchema} > ${SCHEMA_VERSION}). Please update the app.`,
    );
  }
}

/** Bring an op written with any older schema up to the current one. Throws for newer ones. */
export function upcastOp<T extends AnyOp>(op: T): T {
  if (op.schema > SCHEMA_VERSION) throw new UnsupportedSchemaError(op.schema);
  let current: AnyOp = op;
  for (const step of MIGRATIONS) {
    if (step.version > current.schema && step.upcastOp) current = step.upcastOp(current);
    else if (step.version > current.schema) current = { ...current, schema: step.version };
  }
  return current as T;
}
