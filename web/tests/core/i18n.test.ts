import { describe, expect, it } from 'vitest';
import {
  buildCatalogs,
  catalogs,
  createI18n,
  errorCatalogs,
  errorMessage,
  formatEventTime,
  formatMoney,
  formatNumber,
  interpolate,
  isLocale,
  pickLocale,
  translate,
} from '../../src/core/i18n';

describe('pickLocale (06-web.md section 2.5)', () => {
  it('prefers the user setting', () => {
    expect(pickLocale('de', ['en-US'])).toBe('de');
  });

  it('falls back to the first supported browser language by primary subtag', () => {
    expect(pickLocale(null, ['fr-FR', 'de-CH', 'en'])).toBe('de');
    expect(pickLocale(undefined, ['EN-gb'])).toBe('en');
    expect(pickLocale('fr', ['it', ''])).toBe('en');
  });

  it('defaults to English', () => {
    expect(pickLocale(null, [])).toBe('en');
  });

  it('knows the locales', () => {
    expect(isLocale('de')).toBe(true);
    expect(isLocale('fr')).toBe(false);
    expect(isLocale(1)).toBe(false);
  });
});

describe('catalogs', () => {
  it('merges area files under <area>.<key>', () => {
    const merged = buildCatalogs({
      './catalog/b.en.json': { title: 'B' },
      './catalog/a.en.json': { title: 'A' },
      './catalog/a.de.json': { title: 'A de' },
    });
    expect(merged).toEqual({ de: { 'a.title': 'A de' }, en: { 'a.title': 'A', 'b.title': 'B' } });
  });

  it.each([
    ['a wrong file name', { './catalog/a.json': {} }, 'named <area>.<locale>.json'],
    ['an unsupported locale', { './catalog/a.fr.json': {} }, 'named <area>.<locale>.json'],
    ['an upper-case area', { './catalog/A.en.json': {} }, 'named <area>.<locale>.json'],
    ['a list', { './catalog/a.en.json': [] }, 'flat object of texts'],
    ['a nested object', { './catalog/a.en.json': { x: { y: 'z' } } }, '"x" is not a text'],
  ])('refuses %s', (_label, modules, message) => {
    expect(() => buildCatalogs(modules)).toThrow(message);
  });

  it('loads the shipped catalogs', () => {
    expect(catalogs.en['notfound.title']).toBe('Page not found');
    expect(catalogs.de['notfound.title']).toBe('Seite nicht gefunden');
  });

  it('interpolates placeholders and keeps unknown ones', () => {
    expect(interpolate('Code {code} at {n}', { code: 'x', n: 3 })).toBe('Code x at 3');
    expect(interpolate('Keep {missing}')).toBe('Keep {missing}');
  });

  it('falls back to English, then to the key', () => {
    const local = { de: { 'a.x': 'X de' }, en: { 'a.x': 'X', 'a.y': 'Y' } };
    expect(translate(local, 'de', 'a.x')).toBe('X de');
    expect(translate(local, 'de', 'a.y')).toBe('Y');
    expect(translate(local, 'de', 'a.z')).toBe('a.z');
  });
});

describe('error messages', () => {
  it('returns the catalog entry of a known code', () => {
    expect(errorMessage(errorCatalogs, catalogs.de, 'de', 'platform_root_mismatch')).toEqual({
      code: 'platform_root_mismatch',
      known: true,
      ...errorCatalogs.de.platform_root_mismatch,
    });
  });

  it('shows the code for an unknown code', () => {
    const message = errorMessage(errorCatalogs, catalogs.en, 'en', 'brand_new');
    expect(message).toMatchObject({ code: 'brand_new', known: false, title: 'Unexpected error' });
    expect(message.body).toContain('brand_new');
  });

  it('ignores inherited object keys', () => {
    expect(errorMessage(errorCatalogs, catalogs.en, 'en', 'toString').known).toBe(false);
  });

  it('falls back to the code without the generic texts', () => {
    expect(errorMessage(errorCatalogs, {}, 'en', 'brand_new')).toMatchObject({
      title: 'brand_new',
      body: 'brand_new',
      action: 'brand_new',
    });
  });
});

describe('formats', () => {
  it('formats numbers per locale', () => {
    expect(formatNumber(1234.5, 'en')).toBe('1,234.5');
    expect(formatNumber(1234.5, 'de')).toBe('1.234,5');
  });

  it('formats money with four decimals below one dollar', () => {
    expect(formatMoney(0.01234, 'en')).toBe('$0.0123');
    expect(formatMoney(-0.5, 'en')).toBe('-$0.5000');
    expect(formatMoney(12.5, 'en')).toBe('$12.50');
  });

  it('formats event times with milliseconds and the date when not today', () => {
    const at = Date.UTC(2026, 10, 21, 13, 4, 5, 6);
    const options = { locale: 'en' as const, timeZone: 'UTC', now: at + 1000 };
    expect(formatEventTime(at, options)).toBe('13:04:05.006');
    expect(formatEventTime(at, { ...options, now: at + 86_400_000 })).toBe('11/21/2026 13:04:05.006');
    expect(formatEventTime(at, { ...options, locale: 'de', timeZone: 'Europe/Zurich' })).toBe('14:04:05,006');
  });
});

describe('createI18n', () => {
  it('binds translation and errors to the locale', () => {
    const i18n = createI18n('de');
    expect(i18n.locale).toBe('de');
    expect(i18n.t('app.error.code', { code: 'x' })).toBe('Fehlercode: x');
    expect(i18n.error('unavailable').title).toBe('Dienst nicht verfügbar');
  });
});
