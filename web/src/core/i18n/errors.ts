// Error catalog (06-web.md section 2.5): `errors.<locale>.json` is generated from contracts/enums/ by
// scripts/gen-errors.mjs. Unknown codes render a generic message that shows the code.
import type { Catalog } from './catalog';
import { interpolate } from './catalog';
import type { Locale } from './locale';

export interface ErrorText {
  readonly title: string;
  readonly body: string;
  readonly action: string;
}

export interface ErrorMessage extends ErrorText {
  readonly code: string;
  readonly known: boolean;
}

export type ErrorCatalog = Readonly<Record<string, ErrorText>>;

export function errorMessage(
  errors: Record<Locale, ErrorCatalog>,
  messages: Catalog,
  locale: Locale,
  code: string,
): ErrorMessage {
  const catalog = errors[locale];
  if (Object.hasOwn(catalog, code)) return { code, known: true, ...catalog[code]! };
  const generic = (field: keyof ErrorText) => interpolate(messages[`app.error-unknown.${field}`] ?? code, { code });
  return { code, known: false, title: generic('title'), body: generic('body'), action: generic('action') };
}
