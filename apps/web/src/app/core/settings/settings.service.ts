import { Injectable, signal } from '@angular/core';

const KEY = 'pa.settings.v1';

export interface Settings {
  /** Install new app versions automatically when they are ready (default on). */
  autoUpdate: boolean;
  /** One-time hints already shown. */
  helpHintShown: boolean;
  /** Channel to follow ("stable" today; "beta" would be a second CDN path + manifest channel). */
  channel: string;
}

const DEFAULTS: Settings = { autoUpdate: true, helpHintShown: false, channel: 'stable' };

/** Small device-local preferences (localStorage — survives updates, never synced). */
@Injectable({ providedIn: 'root' })
export class SettingsService {
  readonly settings = signal<Settings>(this.load());
  readonly autoUpdate = () => this.settings().autoUpdate;

  private load(): Settings {
    try {
      return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) ?? '{}') };
    } catch {
      return { ...DEFAULTS };
    }
  }

  update(patch: Partial<Settings>): void {
    const next = { ...this.settings(), ...patch };
    this.settings.set(next);
    try {
      localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      /* private mode / quota — keep in memory */
    }
  }
}
