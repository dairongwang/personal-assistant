import {
  ApplicationRef,
  Injectable,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { SwUpdate, VersionEvent } from '@angular/service-worker';
import { filter, first } from 'rxjs';
import { ApiService } from '../account/api.service';
import { RELEASE } from '../release-info';
import { SettingsService } from '../settings/settings.service';
import { UiStateService } from '../ui/ui-state.service';
import {
  type BuildAppData,
  type Decision,
  type ReleaseManifest,
  type SwPhase,
  decide,
  describeUpdate,
} from './update-policy';

const CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
const MIN_CHECK_GAP_MS = 60 * 1000;
const REQUIRED_RETRY_MS = 20 * 1000; // while an update is required but not downloaded yet (CDN lag)

/**
 * Application-code updates.
 *
 * The Angular service worker compares ngsw.json on the CDN with what it cached. When a newer
 * build exists it downloads it in the background (VERSION_DETECTED → VERSION_READY). Policy:
 *  - auto-update (default on): the new build is activated and the app reloads as soon as the
 *    user is not in the middle of something (dialog open, typing). When the app is *reopened*,
 *    the service worker already serves the newest build, so reopening = updated.
 *  - critical releases / unsupported versions (release manifest, SW appData, or a 426 from the
 *    API): a blocking overlay, activation as soon as the build is downloaded.
 */
@Injectable({ providedIn: 'root' })
export class UpdateService {
  private readonly sw = inject(SwUpdate);
  private readonly api = inject(ApiService);
  private readonly settings = inject(SettingsService);
  private readonly ui = inject(UiStateService);
  private readonly appRef = inject(ApplicationRef);

  readonly currentVersion = RELEASE.version;
  readonly currentSchema = RELEASE.schemaVersion;
  readonly swPhase = signal<SwPhase>(this.sw.isEnabled ? 'idle' : 'unsupported');
  readonly readyBuild = signal<BuildAppData | null>(null);
  readonly manifest = signal<ReleaseManifest | null>(null);
  readonly manifestError = signal<string | null>(null);
  readonly error = signal<string | null>(null);
  readonly lastChecked = signal<Date | null>(null);
  readonly online = signal(navigator.onLine);
  readonly activating = signal(false);

  readonly autoUpdate = computed(() => this.settings.settings().autoUpdate);
  readonly decision = computed<Decision>(() =>
    decide({
      currentVersion: this.currentVersion,
      currentSchema: this.currentSchema,
      swPhase: this.swPhase(),
      readyBuild: this.readyBuild(),
      manifest: this.manifest(),
      serverSaysUpgrade: this.api.upgradeRequired(),
      autoUpdate: this.settings.settings().autoUpdate,
      safeToReload: this.ui.safeToReload(),
      online: this.online(),
    }),
  );
  readonly tooltip = computed(() =>
    describeUpdate(
      this.decision(),
      {
        currentVersion: this.currentVersion,
        swPhase: this.swPhase(),
        autoUpdate: this.settings.settings().autoUpdate,
        online: this.online(),
      },
      this.lastChecked(),
      this.error(),
    ),
  );
  readonly attention = computed(() => {
    const d = this.decision();
    return d.required || d.ready || !!d.availableVersion || !!this.error();
  });

  private lastSwCheck = 0;

  constructor() {
    // React to policy changes: apply when allowed, trigger a SW check when something is known to be newer.
    effect(() => {
      const d = this.decision();
      untracked(() => {
        if (d.applyNow) void this.applyNow();
        else if (d.checkNow) void this.checkServiceWorker();
      });
    });
  }

  init(): void {
    window.addEventListener('online', () => {
      this.online.set(true);
      void this.checkNow();
    });
    window.addEventListener('offline', () => this.online.set(false));
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) void this.checkNow();
    });
    setInterval(() => void this.checkNow(), CHECK_INTERVAL_MS);
    setInterval(() => {
      const d = this.decision();
      if ((d.required || d.availableVersion) && !d.ready) void this.checkServiceWorker(true);
    }, REQUIRED_RETRY_MS);

    if (this.sw.isEnabled) {
      this.sw.versionUpdates.subscribe((evt: VersionEvent) => {
        switch (evt.type) {
          case 'VERSION_DETECTED':
            this.swPhase.set('downloading');
            this.readyBuild.set((evt.version.appData as BuildAppData) ?? { version: 'new' });
            break;
          case 'VERSION_READY':
            this.swPhase.set('ready');
            this.readyBuild.set((evt.latestVersion.appData as BuildAppData) ?? { version: 'new' });
            this.error.set(null);
            break;
          case 'VERSION_INSTALLATION_FAILED':
            this.swPhase.set('failed');
            this.error.set(friendlyInstallError(evt.error));
            break;
          case 'NO_NEW_VERSION_DETECTED':
            if (this.swPhase() === 'checking') this.swPhase.set('idle');
            break;
        }
      });
      this.sw.unrecoverable.subscribe(() => {
        // The cached build is broken (e.g. files evicted); a reload fetches a fresh one.
        location.reload();
      });
      // First check once the app is stable (don't compete with startup work).
      this.appRef.isStable
        .pipe(
          filter((s) => s),
          first(),
        )
        .subscribe(() => void this.checkNow());
    } else {
      void this.fetchManifest();
    }
  }

  /** User-triggered or periodic: manifest + service-worker check. */
  async checkNow(): Promise<void> {
    if (!this.online()) return;
    await this.fetchManifest();
    await this.checkServiceWorker(true);
  }

  private async fetchManifest(): Promise<void> {
    try {
      const m = await this.api.get<ReleaseManifest | null>(
        `/release/manifest?channel=${encodeURIComponent(this.api.channel())}`,
        { auth: false },
      );
      this.manifest.set(m);
      this.manifestError.set(null);
    } catch (e) {
      this.manifestError.set(e instanceof Error ? e.message : String(e));
    }
    this.lastChecked.set(new Date());
  }

  private async checkServiceWorker(force = false): Promise<void> {
    if (!this.sw.isEnabled || this.swPhase() === 'ready' || this.swPhase() === 'activating') return;
    if (!force && Date.now() - this.lastSwCheck < MIN_CHECK_GAP_MS) return;
    this.lastSwCheck = Date.now();
    this.swPhase.set('checking');
    try {
      const found = await this.sw.checkForUpdate();
      if (!found && this.swPhase() === 'checking') this.swPhase.set('idle');
    } catch (e) {
      this.swPhase.set('failed');
      this.error.set(friendlyInstallError(e));
    }
  }

  /** Activate the downloaded build and reload so the whole app runs the new code. */
  async applyNow(): Promise<void> {
    if (this.activating()) return;
    this.activating.set(true);
    this.swPhase.set('activating');
    try {
      if (this.sw.isEnabled) {
        const ok = await this.sw.activateUpdate();
        if (!ok && !this.decision().required) {
          // Nothing to activate (already newest) — refresh state instead of reloading.
          this.swPhase.set('idle');
          this.readyBuild.set(null);
          this.activating.set(false);
          return;
        }
      }
      location.reload();
    } catch (e) {
      this.activating.set(false);
      this.swPhase.set('failed');
      this.error.set(friendlyInstallError(e));
    }
  }

  setAutoUpdate(enabled: boolean): void {
    this.settings.update({ autoUpdate: enabled });
  }
}

function friendlyInstallError(e: unknown): string {
  const text = e instanceof Error ? e.message : String(e ?? '');
  if (/quota|storage/i.test(text))
    return 'There is not enough storage space on this device to download the update. Free some space and try again.';
  if (/hash|mismatch|integrity/i.test(text))
    return 'The download was incomplete (the files changed while downloading). This usually fixes itself — try again in a minute.';
  if (/fetch|network|offline/i.test(text))
    return 'The update could not be downloaded. Check your connection and try again.';
  return text
    ? `The update could not be installed (${text}).`
    : 'The update could not be installed.';
}
