// Message catalogs (ADR-027 section 4): every file `catalog/<area>.<locale>.json` is a flat object of message keys
// to texts. Keys are addressed as `<area>.<key>`, so areas never collide. Texts may contain `{name}` placeholders.
import { baseName, isRecord, type ModuleMap } from '../registry';
import { LOCALES, isLocale, type Locale } from './locale';

export type Catalog = Readonly<Record<string, string>>;
export type Params = Readonly<Record<string, string | number>>;

const FILE_RE = /^([a-z][a-z0-9-]*)\.([a-z]{2})$/;

/** Merges the area files of `modules` (glob result with default imports) into one catalog per locale. */
export function buildCatalogs(modules: ModuleMap): Record<Locale, Catalog> {
  const catalogs = Object.fromEntries(LOCALES.map((locale) => [locale, {}])) as Record<Locale, Record<string, string>>;
  for (const path of Object.keys(modules).sort()) {
    const match = FILE_RE.exec(baseName(path, '.json'));
    const locale = match?.[2];
    if (!match || !isLocale(locale)) throw new Error(`${path}: catalog files are named <area>.<locale>.json`);
    const messages = modules[path];
    if (!isRecord(messages)) throw new Error(`${path}: a catalog is a flat object of texts`);
    for (const [key, text] of Object.entries(messages)) {
      if (typeof text !== 'string') throw new Error(`${path}: "${key}" is not a text`);
      catalogs[locale][`${match[1]}.${key}`] = text;
    }
  }
  return catalogs;
}

/** Replaces `{name}` by `params.name`; unknown placeholders stay visible. */
export function interpolate(text: string, params: Params = {}): string {
  return text.replace(/\{([a-z][a-zA-Z0-9_]*)\}/g, (whole, name: string) =>
    name in params ? String(params[name]) : whole,
  );
}

/** Text of `key`, falling back to English and then to the key itself. */
export function translate(catalogs: Record<Locale, Catalog>, locale: Locale, key: string, params?: Params): string {
  const text = catalogs[locale][key] ?? catalogs.en[key] ?? key;
  return interpolate(text, params);
}
