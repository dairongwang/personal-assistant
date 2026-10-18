import { AssistantDb } from './db';
import { MIGRATIONS, SCHEMA_VERSION, UnsupportedSchemaError, upcastOp } from './migrations';
import release from '../../../../release.json';

describe('migrations', () => {
  it('release.json schemaVersion matches the code (bump both together)', () => {
    expect(release.schemaVersion).toBe(SCHEMA_VERSION);
    expect(MIGRATIONS[MIGRATIONS.length - 1].version).toBe(SCHEMA_VERSION);
    expect(MIGRATIONS.map((m) => m.version)).toEqual([...MIGRATIONS.keys()].map((i) => i + 1));
  });

  it('upgrades a v1 database on the device without losing rows', async () => {
    const name = `test-upgrade-${Math.random()}`;
    // Simulate an older app version: only migration steps up to v1.
    const { default: Dexie } = await import('dexie');
    const old = new Dexie(name);
    old.version(1).stores(MIGRATIONS[0].stores);
    await old.open();
    await old.table('tasks').add({
      id: 't1',
      title: 'Legacy',
      status: 'done',
      updatedAt: '2025-01-01T00:00:00Z',
      seq: 0,
    });
    old.close();

    const db = new AssistantDb(name);
    const task = await db.tasks.get('t1');
    expect(db.verno).toBe(SCHEMA_VERSION);
    expect(task?.title).toBe('Legacy');
    expect(task?.tags).toEqual([]); // v2 default
    expect(task?.completedAt).toBe('2025-01-01T00:00:00Z'); // derived for done tasks
    db.close();
  });

  it('up-casts ops written by older app versions', () => {
    const v1 = {
      opId: 'x',
      schema: 1,
      entity: 'task',
      entityId: 't',
      kind: 'put',
      ts: '',
      data: { id: 't', title: 'A', status: 'todo' },
    };
    const op = upcastOp(v1 as never) as { schema: number; data: Record<string, unknown> };
    expect(op.schema).toBe(SCHEMA_VERSION);
    expect(op.data['tags']).toEqual([]);
    expect(op.data['completedAt']).toBeNull();
  });

  it('refuses ops from a newer app version (the app must update instead)', () => {
    const future = {
      opId: 'x',
      schema: SCHEMA_VERSION + 1,
      entity: 'task',
      entityId: 't',
      kind: 'put',
      ts: '',
      data: {},
    };
    expect(() => upcastOp(future as never)).toThrow(UnsupportedSchemaError);
  });
});
