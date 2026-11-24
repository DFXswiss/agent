// The guide for AI agents (ADR-056 section 8): web/public/agents.md is a byte-identical copy of docs/platform/agents.md,
// copied by Vite into the build output, where the hub serves it as `/agents.md`.
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WEB_DIR } from '../helpers';

describe('agents.md', () => {
  it('is a byte-identical copy of docs/platform/agents.md', () => {
    const copy = readFileSync(join(WEB_DIR, 'public', 'agents.md'));
    const source = readFileSync(join(WEB_DIR, '..', 'docs', 'platform', 'agents.md'));
    expect(copy.equals(source)).toBe(true);
  });

  it('is the only file of public/ and Vite copies public/', () => {
    expect(readdirSync(join(WEB_DIR, 'public'))).toEqual(['agents.md']);
    expect(readFileSync(join(WEB_DIR, 'vite.config.ts'), 'utf8')).toContain("publicDir: 'public'");
  });
});
