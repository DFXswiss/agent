// Rule E-19 for names (ADR-073): the interface never names a person, a device, a workgroup, an organization or a
// task by a technical ID. Two permanent checks keep the class out: no message of the catalogs carries an ID (`{id}`)
// or an ID fragment ("…{x}"), except the explicit "ID: …" line shown on request and the listed technical messages;
// and no source file under `src/ui` cuts an ID for display (`<something>Id.slice(…)`), except the listed technical
// ones. A new view that needs a fallback uses `ui/names/names.tsx` (a sentence, the ID on request).
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WEB_DIR } from '../helpers';

const CATALOG_DIR = join(WEB_DIR, 'src/core/i18n/catalog');
const UI_DIR = join(WEB_DIR, 'src/ui');

/** Messages that may carry an ID, with the reason. */
const ID_MESSAGES: Readonly<Record<string, string>> = {
  'names.id.value': 'the ID shown on request, beside the name or the sentence',
  'operations.duplicate-of': 'an operation is technical and has no name; the ID is what the user compares',
};

/** Source lines under `src/ui` that may cut an ID, with the reason. */
const ID_CUTS: Readonly<Record<string, string>> = {
  'task-tabs/charges/ChargesTab.tsx: session: row.sessionId.slice(0, 8),':
    'a session is no person, device, workgroup or organization and has no name; the charges table tells them apart',
};

const ID_CUT = /[A-Za-z_.]*(?:[iI]d|_id|ID)[!?]?\)?\.slice\(/;

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : /\.tsx?$/.test(name) ? [path] : [];
  });
}

describe('no technical ID instead of a name', () => {
  it('no catalog message shows an ID or an ID fragment', () => {
    const found: string[] = [];
    for (const name of readdirSync(CATALOG_DIR)) {
      const area = name.split('.')[0]!;
      const catalog = JSON.parse(readFileSync(join(CATALOG_DIR, name), 'utf8')) as Record<string, string>;
      for (const [key, text] of Object.entries(catalog)) {
        const id = `${area}.${key}`;
        if ((/\{id\}/.test(text) || /…\{/.test(text)) && !(id in ID_MESSAGES)) found.push(`${name}: ${key}`);
      }
    }
    expect(found).toEqual([]);
  });

  it('no screen cuts an ID for display', () => {
    const found: string[] = [];
    for (const path of files(UI_DIR)) {
      for (const line of readFileSync(path, 'utf8').split('\n')) {
        const entry = `${relative(UI_DIR, path)}: ${line.trim()}`;
        if (ID_CUT.test(line) && !(entry in ID_CUTS)) found.push(entry);
      }
    }
    expect(found).toEqual([]);
  });

  it('the allowed exceptions still exist (a stale entry is removed)', () => {
    const lines = new Set(
      files(UI_DIR).flatMap((path) =>
        readFileSync(path, 'utf8')
          .split('\n')
          .map((line) => `${relative(UI_DIR, path)}: ${line.trim()}`),
      ),
    );
    for (const entry of Object.keys(ID_CUTS)) expect(lines.has(entry)).toBe(true);
    const keys = new Set(
      readdirSync(CATALOG_DIR).flatMap((name) =>
        Object.keys(JSON.parse(readFileSync(join(CATALOG_DIR, name), 'utf8'))).map((k) => `${name.split('.')[0]}.${k}`),
      ),
    );
    for (const id of Object.keys(ID_MESSAGES)) expect(keys.has(id)).toBe(true);
  });

  it('finds what it is meant to find', () => {
    expect(ID_CUT.test("t('x', { id: row.id.slice(-6) })")).toBe(true);
    expect(ID_CUT.test('principal_id!.slice(0, 8)')).toBe(true);
    expect(ID_CUT.test('text.slice(0, 8)')).toBe(false);
  });
});
