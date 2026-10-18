/**
 * Pure decision logic for application updates (unit-tested; no Angular/browser APIs).
 *
 * Inputs: what the running app is, what the service worker has found, what the server's
 * release manifest says, and the user's auto-update preference. Output: what to do now.
 */
import { compareVersions, isNewer } from '../version';

export interface ReleaseManifest {
  channel: string;
  version: string;
  schema_version: number;
  min_supported_version: string;
  min_supported_schema: number;
  critical: boolean;
  notes: string;
  published_at: string | null;
}

/** Data the service worker attaches to a build (ngsw appData, written from release.json). */
export interface BuildAppData {
  version?: string;
  schemaVersion?: number;
  critical?: boolean;
  minSupportedVersion?: string;
  notes?: string;
}

export type SwPhase =
  'unsupported' | 'idle' | 'checking' | 'downloading' | 'ready' | 'failed' | 'activating';

export interface PolicyInput {
  currentVersion: string;
  currentSchema: number;
  swPhase: SwPhase;
  /** appData of the version the service worker downloaded (if any). */
  readyBuild: BuildAppData | null;
  manifest: ReleaseManifest | null;
  /** A 426 from the API, if one happened. */
  serverSaysUpgrade: string | null;
  autoUpdate: boolean;
  /** No dialog open, user not typing, no sync running. */
  safeToReload: boolean;
  online: boolean;
}

export interface Decision {
  /** Newer version known (from the SW or the manifest), even if not downloaded yet. */
  availableVersion: string | null;
  /** A newer build is downloaded and can be activated right now. */
  ready: boolean;
  /** The user must update before continuing (critical release / unsupported version). */
  required: boolean;
  reason: string | null;
  /** Activate + reload immediately. */
  applyNow: boolean;
  /** Ask the service worker to look for a new build. */
  checkNow: boolean;
}

export function decide(i: PolicyInput): Decision {
  const m = i.manifest;
  let required = false;
  let reason: string | null = null;

  if (i.serverSaysUpgrade) {
    required = true;
    reason = i.serverSaysUpgrade;
  } else if (m && compareVersions(i.currentVersion, m.min_supported_version) < 0) {
    required = true;
    reason = `Versions older than ${m.min_supported_version} are no longer supported.`;
  } else if (m && i.currentSchema < m.min_supported_schema) {
    required = true;
    reason = 'This version uses an outdated data format.';
  } else if (m && m.critical && isNewer(m.version, i.currentVersion)) {
    required = true;
    reason = `Version ${m.version} is a required update.`;
  } else if (
    i.readyBuild?.critical &&
    i.readyBuild.version &&
    isNewer(i.readyBuild.version, i.currentVersion)
  ) {
    required = true;
    reason = `Version ${i.readyBuild.version} is a required update.`;
  }

  const candidates = [m?.version, i.readyBuild?.version].filter(
    (v): v is string => !!v && isNewer(v, i.currentVersion),
  );
  const availableVersion = candidates.sort(compareVersions).pop() ?? null;
  const ready = i.swPhase === 'ready';
  const applyNow = ready && (required || (i.autoUpdate && i.safeToReload));
  const checkNow =
    i.online &&
    i.swPhase !== 'unsupported' &&
    !ready &&
    i.swPhase !== 'downloading' &&
    i.swPhase !== 'checking' &&
    (required || !!availableVersion);

  return { availableVersion, ready, required, reason, applyNow, checkNow };
}

/** Human text for the Update button's tooltip — always describes what is pending. */
export function describeUpdate(
  d: Decision,
  i: Pick<PolicyInput, 'currentVersion' | 'swPhase' | 'autoUpdate' | 'online'>,
  lastChecked: Date | null,
  error: string | null,
): string {
  const auto = i.autoUpdate ? 'Auto-update is on.' : 'Auto-update is off.';
  if (d.required)
    return `Update required: ${d.reason ?? ''} ${d.ready ? 'Tap to install it now.' : 'Downloading the new version…'}`;
  if (!i.online)
    return `You're offline — updates are checked when you're back online. You're on v${i.currentVersion}.`;
  if (i.swPhase === 'unsupported')
    return `Automatic updates are not available in this browser (no service worker). You're on v${i.currentVersion}.`;
  if (error) return `The last update could not be installed: ${error} Tap to try again.`;
  if (i.swPhase === 'activating') return 'Installing the new version… the app reloads in a moment.';
  if (d.ready)
    return `Version ${d.availableVersion ?? ''} is downloaded and ready. ${i.autoUpdate ? 'It installs automatically when you are not in the middle of something — or tap to install now.' : 'Tap to install it.'}`;
  if (i.swPhase === 'downloading')
    return `Downloading version ${d.availableVersion ?? ''}… ${auto}`;
  if (d.availableVersion)
    return `Update available: v${d.availableVersion}. ${auto} Tap for details.`;
  if (i.swPhase === 'checking') return 'Checking for updates…';
  const when = lastChecked ? ` Last checked ${relative(lastChecked)}.` : '';
  return `You're up to date (v${i.currentVersion}).${when} ${auto} Tap to check again.`;
}

export function relative(date: Date): string {
  const diff = Math.round((Date.now() - date.getTime()) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  return date.toLocaleDateString();
}
