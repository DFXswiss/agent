// Parity of the message catalogs per area and of the error catalog, and completeness of the error catalog against
// the closed code lists of contracts/enums/ (06-web.md section 2.5, ADR-027 section 4).
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { LOCALES } from '../../src/core/i18n';
import { WEB_DIR } from '../helpers';

const I18N_DIR = join(WEB_DIR, 'src/core/i18n');
const CATALOG_DIR = join(I18N_DIR, 'catalog');
const ENUMS_DIR = join(WEB_DIR, '../contracts/enums');

const readJson = (path: string): Record<string, unknown> => JSON.parse(readFileSync(path, 'utf8'));

const areas = [...new Set(readdirSync(CATALOG_DIR).map((name) => name.split('.')[0]!))].sort();

describe('area catalogs', () => {
  it('exist', () => {
    expect(areas).toEqual(expect.arrayContaining(['app', 'notfound']));
  });

  it.each(areas)('%s has one file per locale with the same keys and non-empty texts', (area) => {
    const files = readdirSync(CATALOG_DIR).filter((name) => name.startsWith(`${area}.`));
    expect(files.sort()).toEqual(LOCALES.map((locale) => `${area}.${locale}.json`).sort());
    const [first, ...rest] = LOCALES.map((locale) => readJson(join(CATALOG_DIR, `${area}.${locale}.json`)));
    for (const other of rest) expect(Object.keys(other).sort()).toEqual(Object.keys(first!).sort());
    for (const catalog of [first!, ...rest]) {
      for (const text of Object.values(catalog)) expect(typeof text === 'string' && text.trim() !== '').toBe(true);
    }
  });
});

describe('error catalog', () => {
  const catalogs = LOCALES.map((locale) => readJson(join(I18N_DIR, `errors.${locale}.json`)));

  it('has the same codes in every locale with title, body and action', () => {
    for (const catalog of catalogs) {
      expect(Object.keys(catalog)).toEqual(Object.keys(catalogs[0]!));
      for (const entry of Object.values(catalog)) {
        expect(Object.keys(entry as object)).toEqual(['title', 'body', 'action']);
      }
    }
  });

  it('has an entry for every code of contracts/enums', () => {
    const enums = (name: string) => readJson(join(ENUMS_DIR, name));
    const codes = new Set<string>([
      ...(enums('error-codes.json').codes as { code: string }[]).map((entry) => entry.code),
      ...(enums('operation-error-codes.json').codes as string[]),
      ...(enums('session-failure-reasons.json').reasons as string[]),
      ...(enums('task-states.json').scheduling_reasons as string[]),
    ]);
    expect(codes.size).toBeGreaterThan(100);
    for (const catalog of catalogs) expect(Object.keys(catalog).sort()).toEqual([...codes].sort());
  });
});
