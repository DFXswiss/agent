// Cross-system build comparison (06-web.md section 10.3, decision HOT-18), run by the Linux part of the gates
// (scripts/guest-ci.sh): the same tree is built on the runner device (macOS) and in the Linux VM below another
// directory, with the same SOURCE_DATE_EPOCH, BUILD_COMMIT and BUILD_VERSION; the two build hashes must be equal.
//
//   node scripts/build-compare.mjs build OUT_DIR        build into OUT_DIR, print `build hash <hex>`
//   node scripts/build-compare.mjs compare A B          compare two build-manifest.json files, exit 1 on a difference
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

/** The build hash: SHA-256 of the manifest bytes (as scripts/build.mjs computes it). */
export const buildHash = (text) => createHash('sha256').update(text).digest('hex');

/** Differences between two manifest texts, by file; empty when the bytes are equal. */
export function manifestDifferences(first, second) {
  if (first === second) return [];
  const [a, b] = [JSON.parse(first), JSON.parse(second)];
  const right = new Map(b.files.map((file) => [file.path, file]));
  const left = new Map(a.files.map((file) => [file.path, file]));
  const problems = [];
  for (const file of a.files) {
    const other = right.get(file.path);
    if (!other) problems.push(`${file.path}: only in the first build`);
    else if (other.sha256 !== file.sha256 || other.size !== file.size)
      problems.push(
        `${file.path}: content differs (sha256 ${file.sha256} / ${other.sha256}, size ${file.size} / ${other.size})`,
      );
  }
  for (const file of b.files) if (!left.has(file.path)) problems.push(`${file.path}: only in the second build`);
  for (const key of ['version', 'commit', 'root_key_fingerprint'])
    if (a[key] !== b[key]) problems.push(`${key} differs`);
  return problems.length ? problems : ['manifest bytes differ'];
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const [command, ...args] = process.argv.slice(2);
  if (command === 'build' && args.length === 1) {
    const { buildWeb } = await import('./build.mjs');
    console.log(`build hash ${await buildWeb({ outDir: args[0] })}`);
  } else if (command === 'compare' && args.length === 2) {
    const [first, second] = args.map((path) => readFileSync(path, 'utf8'));
    const problems = manifestDifferences(first, second);
    if (problems.length) {
      console.error(`build hashes differ: ${buildHash(first)} / ${buildHash(second)}\n  ${problems.join('\n  ')}`);
      process.exit(1);
    }
    console.log(`build hashes equal: ${buildHash(first)}`);
  } else {
    console.error('usage: build-compare.mjs build OUT_DIR | compare MANIFEST MANIFEST');
    process.exit(2);
  }
}
