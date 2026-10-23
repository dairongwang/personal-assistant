import { Injectable, signal } from '@angular/core';

const KEY = 'pa.settings.v1';

/** How the sponsored-content panel picks a region (core/ads/region.ts). */
export type AdRegionMode = 'auto' | 'manual' | 'off';

export interface Settings {
  /** Install new app versions automatically when they are ready (default on). */
  autoUpdate: boolean;
  /** One-time hints already shown. */
  helpHintShown: boolean;
  /** Channel to follow ("stable" today; "beta" would be a second CDN path + manifest channel). */
  channel: string;
  /** Sponsored content on this device (free memberships). Off = the panel is gone and nothing is requested. */
  adsEnabled: boolean;
  /** auto = country derived on the device from time zone/locale · manual = `adsRegion` · off = no region. */
  adsRegionMode: AdRegionMode;
  /** ISO 3166-1 alpha-2 chosen by the user (manual mode). */
  adsRegion: string | null;
  /** Panel folded to its one-line strip. */
  adsCollapsed: boolean;
  /** Large showcase layout (≈ one third of the page, targeting highlighted) — the debug/demo view.
   *  `false` = the compact production card. */
  adsLarge: boolean;
}

const DEFAULTS: Settings = {
  autoUpdate: true,
  helpHintShown: false,
  channel: 'stable',
  adsEnabled: true,
  adsRegionMode: 'auto',
  adsRegion: null,
  adsCollapsed: false,
  adsLarge: true,
};

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
