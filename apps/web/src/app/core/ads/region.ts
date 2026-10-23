import { COUNTRY_CODES, ZONE_COUNTRY } from './zone-country';

/**
 * Coarse region detection — on the device, from what the browser already knows.
 *
 * The sponsored-content panel picks creatives by *country*, nothing finer. The country is
 * derived here from the IANA time zone the browser reports (`Europe/Berlin` → `DE`) or, failing
 * that, from the locale's region subtag (`en-GB` → `GB`). No geolocation API, no IP lookup, no
 * permission prompt; the inputs never leave the device — only the resulting two-letter code is
 * sent with the ad request, and the user can see, override or switch it off (AdsService).
 */

export type RegionSource = 'timezone' | 'language' | 'manual' | 'none';

export interface DetectedRegion {
  /** ISO 3166-1 alpha-2, or null when nothing sensible could be derived (UTC, odd locales). */
  region: string | null;
  source: RegionSource;
}

/** "Unknown / no region" (ISO 3166-1 user-assigned code) — what the client sends when the user opted out. */
export const NO_REGION = 'ZZ';

const ALPHA2 = /^[A-Z]{2}$/;

export function isCountryCode(value: string | null | undefined): value is string {
  return !!value && ALPHA2.test(value) && value !== NO_REGION;
}

/** Country of an IANA zone name, or null (`UTC`, `Etc/GMT+2`, unknown names). */
export function countryOfTimeZone(zone: string | null | undefined): string | null {
  if (!zone) return null;
  return ZONE_COUNTRY[zone] ?? null;
}

/** Region subtag of a BCP 47 tag (`de-AT` → `AT`, `zh-Hant-TW` → `TW`, `en` → null). */
export function countryOfLocale(tag: string | null | undefined): string | null {
  if (!tag) return null;
  for (const part of tag.split(/[-_]/).slice(1)) {
    const upper = part.toUpperCase();
    if (ALPHA2.test(upper)) return upper;
  }
  return null;
}

/** Primary language subtag (`de-CH` → `de`), or null. */
export function primaryLanguage(tag: string | null | undefined): string | null {
  const primary = (tag ?? '').split(/[-_]/)[0]?.toLowerCase() ?? '';
  return /^[a-z]{2,3}$/.test(primary) ? primary : null;
}

export function detectRegion(
  input: { timeZone?: string | null; language?: string | null } = browserHints(),
): DetectedRegion {
  const fromZone = countryOfTimeZone(input.timeZone);
  if (fromZone) return { region: fromZone, source: 'timezone' };
  const fromLocale = countryOfLocale(input.language);
  if (fromLocale) return { region: fromLocale, source: 'language' };
  return { region: null, source: 'none' };
}

export function browserHints(): { timeZone: string | null; language: string | null } {
  let timeZone: string | null = null;
  try {
    timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone ?? null;
  } catch {
    /* very old engines */
  }
  return { timeZone, language: typeof navigator !== 'undefined' ? navigator.language : null };
}

/** Human name for a country code in the UI language ("DE" → "Germany"), falling back to the code. */
export function countryName(code: string, locale?: string): string {
  try {
    const names = new Intl.DisplayNames(locale ? [locale] : undefined, { type: 'region' });
    return names.of(code) ?? code;
  } catch {
    return code;
  }
}

/** Human name for a language code ("de" → "German"), falling back to the code. */
export function languageName(code: string, locale?: string): string {
  try {
    const names = new Intl.DisplayNames(locale ? [locale] : undefined, { type: 'language' });
    return names.of(code) ?? code;
  } catch {
    return code;
  }
}

/** All countries for the manual picker, sorted by display name. */
export function countryOptions(locale?: string): { code: string; name: string }[] {
  return COUNTRY_CODES.map((code) => ({ code, name: countryName(code, locale) })).sort((a, b) =>
    a.name.localeCompare(b.name),
  );
}
