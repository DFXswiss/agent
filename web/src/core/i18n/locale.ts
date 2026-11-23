// Locale selection (06-web.md section 2.5): the signed-in user's setting, else the first supported entry of
// `navigator.languages` (matched on the primary subtag, so `de-CH` selects `de`), else English.

export const LOCALES = ['de', 'en'] as const;
export type Locale = (typeof LOCALES)[number];
export const DEFAULT_LOCALE: Locale = 'en';

export function isLocale(value: unknown): value is Locale {
  return typeof value === 'string' && (LOCALES as readonly string[]).includes(value);
}

export function pickLocale(userLocale: string | null | undefined, languages: readonly string[]): Locale {
  if (isLocale(userLocale)) return userLocale;
  for (const language of languages) {
    const primary = language.split('-')[0]?.toLowerCase();
    if (isLocale(primary)) return primary;
  }
  return DEFAULT_LOCALE;
}
