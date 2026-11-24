// Generates the error catalogs src/core/i18n/errors.{de,en}.json (06-web.md section 2.5, ADR-027 section 4).
//
// The list of codes comes only from the closed enums in contracts/enums/; the texts ({title, body, action} per
// locale) are maintained in scripts/error-texts.json. The script refuses a code without texts in every locale and
// texts for a code the contracts do not know, so a contract change cannot silently leave the UI without a message.
//
//   node scripts/gen-errors.mjs          write the catalogs
//   node scripts/gen-errors.mjs --check  exit 1 if the committed catalogs are stale
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB = join(dirname(fileURLToPath(import.meta.url)), '..');
export const ENUMS_DIR = join(WEB, '..', 'contracts', 'enums');
export const TEXTS_FILE = join(WEB, 'scripts', 'error-texts.json');
export const CATALOG_DIR = join(WEB, 'src', 'core', 'i18n');
export const LOCALES = ['de', 'en'];
const FIELDS = ['title', 'body', 'action'];

/** Every enum file that holds codes shown to users, with the accessor of its code list. */
export const ENUM_SOURCES = [
  ['error-codes.json', (data) => data.codes.map((entry) => entry.code)],
  ['operation-error-codes.json', (data) => data.codes],
  ['session-failure-reasons.json', (data) => data.reasons],
  ['task-states.json', (data) => data.scheduling_reasons],
];

const readJson = (path) => JSON.parse(readFileSync(path, 'utf8'));

function readOrNull(path) {
  try {
    return readFileSync(path, 'utf8');
  } catch {
    return null;
  }
}

/** Sorted, unique codes of all enum sources in `enumsDir`. */
export function contractCodes(enumsDir = ENUMS_DIR) {
  const codes = new Set();
  for (const [file, pick] of ENUM_SOURCES) {
    for (const code of pick(readJson(join(enumsDir, file)))) codes.add(code);
  }
  return [...codes].sort();
}

/** One catalog per locale; throws with every problem found. */
export function buildCatalogs(codes, texts) {
  const problems = [];
  const known = new Set(codes);
  for (const code of Object.keys(texts)) {
    if (!known.has(code)) problems.push(`texts for unknown code ${code}`);
  }
  const catalogs = Object.fromEntries(LOCALES.map((locale) => [locale, {}]));
  for (const code of codes) {
    for (const locale of LOCALES) {
      const entry = texts[code]?.[locale];
      const valid = entry && FIELDS.every((field) => typeof entry[field] === 'string' && entry[field].trim() !== '');
      if (!valid || Object.keys(entry).length !== FIELDS.length) {
        problems.push(`${code}: missing or invalid ${locale} texts (need exactly ${FIELDS.join(', ')})`);
        continue;
      }
      catalogs[locale][code] = { title: entry.title, body: entry.body, action: entry.action };
    }
  }
  if (problems.length) throw new Error(`error catalog:\n  ${problems.join('\n  ')}`);
  return catalogs;
}

export const render = (catalog) => `${JSON.stringify(catalog, null, 2)}\n`;
export const catalogFile = (locale, dir = CATALOG_DIR) => join(dir, `errors.${locale}.json`);

/** Writes (or with `check` compares) the catalogs; returns the stale files. */
export function generate({ check = false, enumsDir = ENUMS_DIR, textsFile = TEXTS_FILE, outDir = CATALOG_DIR } = {}) {
  const catalogs = buildCatalogs(contractCodes(enumsDir), readJson(textsFile));
  const stale = [];
  for (const locale of LOCALES) {
    const file = catalogFile(locale, outDir);
    const text = render(catalogs[locale]);
    if (readOrNull(file) === text) continue;
    stale.push(file);
    if (!check) writeFileSync(file, text);
  }
  return stale;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const check = process.argv.includes('--check');
  try {
    const stale = generate({ check });
    if (check && stale.length) {
      console.error(`stale error catalogs (run npm run gen:errors): ${stale.join(', ')}`);
      process.exit(1);
    }
    console.log(check ? 'error catalogs up to date' : `error catalogs written (${stale.length} changed)`);
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
