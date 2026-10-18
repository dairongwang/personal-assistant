import { decide, describeUpdate, type PolicyInput, type ReleaseManifest } from './update-policy';

const manifest = (over: Partial<ReleaseManifest> = {}): ReleaseManifest => ({
  channel: 'stable',
  version: '1.1.0',
  schema_version: 2,
  min_supported_version: '1.0.0',
  min_supported_schema: 2,
  critical: false,
  notes: '',
  published_at: null,
  ...over,
});
const base: PolicyInput = {
  currentVersion: '1.0.0',
  currentSchema: 2,
  swPhase: 'idle',
  readyBuild: null,
  manifest: null,
  serverSaysUpgrade: null,
  autoUpdate: true,
  safeToReload: true,
  online: true,
};

describe('update policy', () => {
  it('is quiet when nothing newer is known', () => {
    const d = decide({ ...base, manifest: manifest({ version: '1.0.0' }) });
    expect(d).toMatchObject({
      availableVersion: null,
      ready: false,
      required: false,
      applyNow: false,
      checkNow: false,
    });
    expect(describeUpdate(d, base, null, null)).toContain("You're up to date (v1.0.0)");
  });

  it('asks the service worker to download when the manifest announces a newer version', () => {
    const d = decide({ ...base, manifest: manifest() });
    expect(d.availableVersion).toBe('1.1.0');
    expect(d.checkNow).toBe(true);
    expect(d.applyNow).toBe(false);
  });

  it('auto-applies a ready build only when it is safe, or on demand when auto is off', () => {
    const ready = { ...base, swPhase: 'ready' as const, readyBuild: { version: '1.1.0' } };
    expect(decide(ready).applyNow).toBe(true);
    expect(decide({ ...ready, safeToReload: false }).applyNow).toBe(false);
    expect(decide({ ...ready, autoUpdate: false }).applyNow).toBe(false);
    expect(
      describeUpdate(
        decide({ ...ready, autoUpdate: false }),
        { ...ready, autoUpdate: false },
        null,
        null,
      ),
    ).toContain('Tap to install it');
  });

  it('forces critical releases and unsupported versions, even with auto-update off or a dialog open', () => {
    const critical = decide({
      ...base,
      manifest: manifest({ critical: true }),
      swPhase: 'ready',
      readyBuild: { version: '1.1.0' },
      autoUpdate: false,
      safeToReload: false,
    });
    expect(critical.required).toBe(true);
    expect(critical.applyNow).toBe(true);
    expect(critical.reason).toContain('required update');

    const tooOld = decide({
      ...base,
      currentVersion: '0.9.0',
      manifest: manifest({ min_supported_version: '1.0.0' }),
    });
    expect(tooOld.required).toBe(true);
    expect(tooOld.reason).toContain('no longer supported');

    const oldSchema = decide({
      ...base,
      currentSchema: 1,
      manifest: manifest({ min_supported_schema: 2 }),
    });
    expect(oldSchema.required).toBe(true);

    const server = decide({ ...base, serverSaysUpgrade: 'Please update the app.' });
    expect(server.required).toBe(true);
    expect(server.checkNow).toBe(true);
  });

  it('reads the critical flag from the downloaded build itself (works without the API)', () => {
    const d = decide({
      ...base,
      swPhase: 'ready',
      readyBuild: { version: '1.0.1', critical: true },
    });
    expect(d.required).toBe(true);
    expect(d.availableVersion).toBe('1.0.1');
  });

  it('does not auto-apply a build that is not newer (rollback protection)', () => {
    const d = decide({ ...base, manifest: manifest({ version: '1.0.0', critical: true }) });
    expect(d.required).toBe(false);
  });

  it('explains offline and unsupported browsers', () => {
    expect(
      describeUpdate(decide({ ...base, online: false }), { ...base, online: false }, null, null),
    ).toContain('offline');
    const unsupported = { ...base, swPhase: 'unsupported' as const };
    expect(describeUpdate(decide(unsupported), unsupported, null, null)).toContain(
      'not available in this browser',
    );
  });
});
