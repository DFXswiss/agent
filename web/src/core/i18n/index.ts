// i18n of the web client (06-web.md section 2.5): area catalogs discovered from `catalog/<area>.<locale>.json`
// (ADR-027 section 4) and the generated error catalog.
import { buildCatalogs, translate, type Catalog, type Params } from './catalog';
import errorsDe from './errors.de.json';
import errorsEn from './errors.en.json';
import { errorMessage, type ErrorCatalog, type ErrorMessage } from './errors';
import type { Locale } from './locale';

export { DEFAULT_LOCALE, LOCALES, isLocale, pickLocale, type Locale } from './locale';
export { buildCatalogs, interpolate, translate, type Catalog, type Params } from './catalog';
export { errorMessage, type ErrorCatalog, type ErrorMessage, type ErrorText } from './errors';
export { formatClock, formatEventTime, formatMoney, formatNumber, localTimeZone, type TimeOptions } from './format';

export const catalogs: Record<Locale, Catalog> = buildCatalogs(
  import.meta.glob('./catalog/*.json', { eager: true, import: 'default' }),
);
export const errorCatalogs: Record<Locale, ErrorCatalog> = { de: errorsDe, en: errorsEn };

export interface I18n {
  readonly locale: Locale;
  t(key: string, params?: Params): string;
  error(code: string): ErrorMessage;
}

export function createI18n(locale: Locale): I18n {
  return {
    locale,
    t: (key, params) => translate(catalogs, locale, key, params),
    error: (code) => errorMessage(errorCatalogs, catalogs[locale], locale, code),
  };
}
