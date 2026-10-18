import Dexie, { type Table } from 'dexie';
import type { CalendarEvent, MetaRow, OutboxEntry, Task } from './models';
import { applyMigrations } from './migrations';

/** The device-local database. Opened lazily; migrations run on first access. */
export class AssistantDb extends Dexie {
  tasks!: Table<Task, string>;
  events!: Table<CalendarEvent, string>;
  outbox!: Table<OutboxEntry, number>;
  meta!: Table<MetaRow, string>;

  constructor(name = 'personal-assistant') {
    super(name);
    applyMigrations(this);
  }

  async getMeta<T>(key: string): Promise<T | undefined> {
    return (await this.meta.get(key))?.value as T | undefined;
  }

  async setMeta(key: string, value: unknown): Promise<void> {
    await this.meta.put({ key, value });
  }
}
