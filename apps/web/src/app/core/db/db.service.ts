import { Injectable } from '@angular/core';
import { AssistantDb } from './db';

/** Injectable wrapper so tests can substitute a fresh database per test. */
@Injectable({ providedIn: 'root' })
export class DbService {
  private instance: AssistantDb | null = null;

  get db(): AssistantDb {
    this.instance ??= new AssistantDb();
    return this.instance;
  }

  /** Replace the database (tests, "reset local cache"). */
  use(db: AssistantDb): void {
    this.instance = db;
  }
}
