import {
  NO_REGION,
  countryName,
  countryOfLocale,
  countryOfTimeZone,
  countryOptions,
  detectRegion,
  isCountryCode,
  languageName,
  primaryLanguage,
} from './region';

describe('Coarse region detection (on-device, no geolocation)', () => {
  it('maps IANA time zones to countries, including legacy aliases', () => {
    expect(countryOfTimeZone('Europe/Berlin')).toBe('DE');
    expect(countryOfTimeZone('America/New_York')).toBe('US');
    expect(countryOfTimeZone('Asia/Kolkata')).toBe('IN');
    expect(countryOfTimeZone('Asia/Calcutta')).toBe('IN'); // older ICU name
    expect(countryOfTimeZone('Europe/Kyiv')).toBe('UA');
    expect(countryOfTimeZone('Europe/Kiev')).toBe('UA');
    expect(countryOfTimeZone('Australia/Sydney')).toBe('AU');
    expect(countryOfTimeZone('Pacific/Auckland')).toBe('NZ');
  });

  it('does not guess from zones that say nothing about a country', () => {
    for (const zone of ['UTC', 'Etc/GMT+2', 'GMT', 'Factory', '', null, undefined]) {
      expect(countryOfTimeZone(zone)).toBeNull();
    }
  });

  it('falls back to the locale region subtag, then to nothing', () => {
    expect(countryOfLocale('de-AT')).toBe('AT');
    expect(countryOfLocale('zh-Hant-TW')).toBe('TW');
    expect(countryOfLocale('en_GB')).toBe('GB');
    expect(countryOfLocale('en')).toBeNull();
    expect(countryOfLocale('')).toBeNull();
    expect(detectRegion({ timeZone: 'Europe/Paris', language: 'en-US' })).toEqual({
      region: 'FR',
      source: 'timezone',
    });
    expect(detectRegion({ timeZone: 'UTC', language: 'pt-BR' })).toEqual({
      region: 'BR',
      source: 'language',
    });
    expect(detectRegion({ timeZone: 'UTC', language: 'en' })).toEqual({
      region: null,
      source: 'none',
    });
    expect(detectRegion({})).toEqual({ region: null, source: 'none' });
  });

  it('extracts the primary language subtag', () => {
    expect(primaryLanguage('de-CH')).toBe('de');
    expect(primaryLanguage('EN')).toBe('en');
    expect(primaryLanguage('ast')).toBe('ast');
    expect(primaryLanguage('x')).toBeNull();
    expect(primaryLanguage(null)).toBeNull();
  });

  it('validates country codes and keeps ZZ as the explicit "no region"', () => {
    expect(isCountryCode('DE')).toBe(true);
    expect(isCountryCode('de')).toBe(false);
    expect(isCountryCode(NO_REGION)).toBe(false);
    expect(isCountryCode(null)).toBe(false);
    expect(isCountryCode('DEU')).toBe(false);
  });

  it('offers every country with a readable name for the manual picker', () => {
    const options = countryOptions('en');
    expect(options.length).toBeGreaterThan(240);
    expect(options.find((o) => o.code === 'DE')?.name).toBe('Germany');
    expect(options.map((o) => o.name)).toEqual(
      [...options.map((o) => o.name)].sort((a, b) => a.localeCompare(b)),
    );
    expect(countryName('FR', 'en')).toBe('France');
    expect(countryName('XX', 'en')).toBe('XX');
    expect(languageName('de', 'en')).toBe('German');
    expect(languageName('zz', 'en')).toBe('zz');
  });
});
