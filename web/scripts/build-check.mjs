// `npm run build:check` (06-web.md sections 10.2, 10.3, 12.2 and 13): builds twice into scratch directories and fails
// unless both outputs are byte-identical, index.html satisfies the CSP/SRI rules (lazily loaded chunks included: the
// asset table and the integrity-checking preload helper, KOR-04), the bundle contains no `__TEST__`
// and assigns no globals, build-manifest.json describes exactly the files written and the initial JavaScript fits the
// budget (`INITIAL_JS_BUDGET`, 219 KiB gzip). Prints the build hash and the initial JS size.
import { mkdirSync, readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compiledRootKey, listFiles, MANIFEST_FILE, rootKeyFingerprint } from '../build/manifest.ts';
import { budgetProblems, INITIAL_JS_BUDGET, initialScripts } from '../build/budget.ts';
import { assetIntegrityProblems, dynamicImportProblems, htmlProblems, listAssets, readFrom } from '../build/sri.ts';
import { buildWeb, WEB } from './build.mjs';

const GLOBAL_ASSIGNMENT =
  /\b(?:window|globalThis|self)\s*(?:\.\s*[A-Za-z_$][\w$]*|\[[^\]]+\])\s*(?:\?\?|\|\||&&)?=(?![=>])|Object\.define(?:Propert(?:y|ies))\(\s*(?:window|globalThis|self)\b/g;

/** Differences between two build outputs (paths and bytes). */
export function compareTrees(a, b) {
  const left = new Map(listFiles(a).map((file) => [file.path, file.sha256]));
  const right = new Map(listFiles(b).map((file) => [file.path, file.sha256]));
  const problems = [];
  for (const path of new Set([...left.keys(), ...right.keys()])) {
    if (left.get(path) !== right.get(path)) problems.push(`not reproducible: ${path}`);
  }
  if (readFileSync(join(a, MANIFEST_FILE), 'utf8') !== readFileSync(join(b, MANIFEST_FILE), 'utf8')) {
    problems.push(`not reproducible: ${MANIFEST_FILE}`);
  }
  return problems;
}

/** Absolute paths of the build machine in any output file, source maps included (06-web.md section 10.3). */
export function absolutePathProblems(dir, roots = [join(WEB, '..'), process.env.HOME ?? '']) {
  const needles = roots.filter((root) => root.length > 1);
  return listFiles(dir)
    .filter(({ path }) => needles.some((needle) => readFileSync(join(dir, path), 'utf8').includes(needle)))
    .map(({ path }) => `${path}: contains an absolute path of the build machine`);
}

/** `__TEST__` markers and global assignments in the delivered code (source maps excluded). */
export function bundleProblems(dir) {
  const problems = [];
  for (const { path } of listFiles(dir)) {
    if (path.endsWith('.map')) continue;
    const text = readFileSync(join(dir, path), 'utf8');
    if (text.includes('__TEST__')) problems.push(`${path}: contains __TEST__`);
    if (/\.(?:js|mjs)$/.test(path)) {
      for (const match of text.matchAll(GLOBAL_ASSIGNMENT)) problems.push(`${path}: assigns a global (${match[0]})`);
    }
  }
  return problems;
}

/** The manifest must list exactly the files on disk and carry the compiled key's fingerprint. */
export function manifestProblems(dir, rootKey) {
  const manifest = JSON.parse(readFileSync(join(dir, MANIFEST_FILE), 'utf8'));
  const problems = [];
  if (JSON.stringify(manifest.files) !== JSON.stringify(listFiles(dir)))
    problems.push('manifest files differ from output');
  if (manifest.root_key_fingerprint !== rootKeyFingerprint(rootKey)) problems.push('manifest root key fingerprint');
  if (!/^[0-9a-f]{40}$/.test(manifest.commit)) problems.push('manifest commit is not a commit hash');
  if (typeof manifest.version !== 'string' || manifest.version === '') problems.push('manifest version missing');
  return problems;
}

export function check(dir, rootKey) {
  const html = readFileSync(join(dir, 'index.html'), 'utf8');
  return [
    ...htmlProblems(html, readFrom(dir)),
    ...assetIntegrityProblems(html, listAssets(dir), readFrom(dir)),
    ...dynamicImportProblems(html, listAssets(dir), readFrom(dir)),
    ...bundleProblems(dir),
    ...absolutePathProblems(dir),
    ...manifestProblems(dir, rootKey),
    ...budgetProblems(dir),
  ];
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const scratch = join(WEB, 'node_modules', '.tmp', 'build-check');
  rmSync(scratch, { recursive: true, force: true });
  const [a, b] = ['a', 'b'].map((name) => join(scratch, name));
  mkdirSync(scratch, { recursive: true });
  const hash = await buildWeb({ outDir: a });
  await buildWeb({ outDir: b });
  const problems = [...compareTrees(a, b), ...check(a, compiledRootKey())];
  if (problems.length) {
    console.error(`build check failed:\n  ${problems.join('\n  ')}`);
    process.exit(1);
  }
  const initial = initialScripts(a).reduce((sum, script) => sum + script.gzip, 0);
  console.log(`build check passed: two builds byte-identical, SRI/CSP rules met, build hash ${hash}`);
  console.log(`initial JS ${(initial / 1024).toFixed(1)} KiB gzip (budget ${INITIAL_JS_BUDGET / 1024} KiB)`);
}
