import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const WEB_DIR = join(dirname(fileURLToPath(import.meta.url)), '..');

export const readSource = (path: string): string => readFileSync(join(WEB_DIR, path), 'utf8');

/** The literal glob pattern of a registration point, as written in its source file. */
export function globPatterns(path: string): string[] {
  return [...readSource(path).matchAll(/import\.meta\.glob\((\[[^\]]*\]|'[^']*')/g)].map((match) => match[1]!);
}
