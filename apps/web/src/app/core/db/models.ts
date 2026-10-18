/** Local data model (IndexedDB). Everything the user creates lives here, on the device. */

export type Priority = 'low' | 'medium' | 'high' | 'urgent';
export type TaskStatus = 'todo' | 'in_progress' | 'done';

export interface Task {
  id: string; // UUID generated on the device
  title: string;
  description: string | null;
  priority: Priority;
  status: TaskStatus;
  dueAt: string | null; // ISO-8601 UTC
  tags: string[];
  completedAt: string | null;
  createdAt: string;
  updatedAt: string;
  /** Server sequence of the last op applied to this row (0 = never synced). */
  seq: number;
}

export interface CalendarEvent {
  id: string;
  title: string;
  description: string | null;
  startAt: string;
  endAt: string;
  location: string | null;
  createdAt: string;
  updatedAt: string;
  seq: number;
}

export type Entity = 'task' | 'event';

/** One mutation. Ops are the unit of sync and of migration (each carries its schema version). */
export interface Op {
  opId: string;
  schema: number;
  entity: Entity;
  entityId: string;
  kind: 'put' | 'delete';
  data: Task | CalendarEvent | null;
  ts: string; // device time, informational only — ordering is decided by the server seq
}

/** A pending op waiting to be pushed (local outbox). */
export interface OutboxEntry {
  localSeq?: number;
  op: Op;
}

export interface MetaRow {
  key: string;
  value: unknown;
}

export const PRIORITY_RANK: Record<Priority, number> = { urgent: 4, high: 3, medium: 2, low: 1 };

export function newId(): string {
  return crypto.randomUUID();
}

export function nowIso(): string {
  return new Date().toISOString();
}
