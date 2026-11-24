import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { buildCatalogs, contractCodes, generate, render } from '../../scripts/gen-errors.mjs';
import { WEB_DIR } from '../helpers';

const TEXT = { title: 'T', body: 'B', action: 'A' };
const both = { de: TEXT, en: TEXT };

function scratch() {
  const base = join(WEB_DIR, 'node_modules', '.tmp');
  mkdirSync(base, { recursive: true });
  const dir = mkdtempSync(join(base, 'gen-errors-'));
  const enums = join(dir, 'enums');
  mkdirSync(enums);
  writeFileSync(join(enums, 'error-codes.json'), JSON.stringify({ codes: [{ code: 'b_code' }, { code: 'a_code' }] }));
  writeFileSync(join(enums, 'operation-error-codes.json'), JSON.stringify({ codes: ['a_code'] }));
  writeFileSync(join(enums, 'session-failure-reasons.json'), JSON.stringify({ reasons: ['c_code'] }));
  writeFileSync(join(enums, 'task-states.json'), JSON.stringify({ scheduling_reasons: [] }));
  const texts = join(dir, 'texts.json');
  writeFileSync(texts, JSON.stringify({ a_code: both, b_code: both, c_code: both }));
  return { dir, enums, texts };
}

describe('gen-errors', () => {
  it('reads sorted unique codes from all enum sources', () => {
    expect(contractCodes(scratch().enums)).toEqual(['a_code', 'b_code', 'c_code']);
  });

  it('writes, then confirms up-to-date catalogs', () => {
    const { dir, enums, texts } = scratch();
    const options = { enumsDir: enums, textsFile: texts, outDir: dir };
    expect(generate({ ...options, check: true })).toHaveLength(2);
    expect(generate(options)).toHaveLength(2);
    expect(generate({ ...options, check: true })).toEqual([]);
    expect(JSON.parse(readFileSync(join(dir, 'errors.de.json'), 'utf8'))).toEqual({
      a_code: TEXT,
      b_code: TEXT,
      c_code: TEXT,
    });
  });

  it('refuses missing, invalid and unknown texts', () => {
    expect(() =>
      buildCatalogs(['a', 'b', 'c', 'd'], {
        a: { de: TEXT },
        b: { de: { ...TEXT, title: ' ' }, en: TEXT },
        c: { de: { ...TEXT, extra: 'x' }, en: TEXT },
        d: both,
        z: both,
      }),
    ).toThrow(
      /unknown code z[\s\S]*a: missing or invalid en[\s\S]*b: missing or invalid de[\s\S]*c: missing or invalid de/,
    );
  });

  it('renders stable JSON', () => {
    expect(render({ a: TEXT })).toBe(`${JSON.stringify({ a: TEXT }, null, 2)}\n`);
  });

  it('keeps the committed catalogs in sync with the contracts', () => {
    expect(generate({ check: true })).toEqual([]);
  });
});
