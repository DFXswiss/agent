// `npm run build`: reproducible production build (06-web.md section 10.3). SOURCE_DATE_EPOCH defaults to the commit
// time and BUILD_COMMIT to the commit of HEAD, so two builds of the same commit are byte-identical.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';
import { compiledRootKey, TEST_ROOT_KEY } from '../build/manifest.ts';

export const WEB = join(dirname(fileURLToPath(import.meta.url)), '..');
export const OUT_DIR = join(WEB, '..', 'src', 'agent_hub', 'static');

const git = (...args) => execFileSync('git', ['-C', WEB, ...args], { encoding: 'utf8' }).trim();

/**
 * Refusals of a release build (`npm run build -- --release`): the test root key, a missing key and uncommitted
 * changes (the manifest records HEAD, so the build hash must belong to exactly that commit).
 */
export function releaseProblems(env = process.env, status = () => git('status', '--porcelain')) {
  const problems = [];
  if (!env.VITE_PLATFORM_ROOT_KEY || compiledRootKey(env) === TEST_ROOT_KEY) {
    problems.push('VITE_PLATFORM_ROOT_KEY must be set to the production platform root key');
  }
  if (status() !== '') problems.push('the working tree has uncommitted changes');
  return problems;
}

/** Builds into `outDir` (default: the hub's static directory) and returns the build hash. */
export async function buildWeb({ outDir = OUT_DIR, logLevel = 'warn', minify = true } = {}) {
  process.env.SOURCE_DATE_EPOCH ??= git('log', '-1', '--format=%ct');
  process.env.BUILD_COMMIT ??= git('rev-parse', 'HEAD');
  process.env.BUILD_VERSION ??= JSON.parse(readFileSync(join(WEB, 'package.json'), 'utf8')).version;
  // The unminified build of the coverage run keeps library comments otherwise; markers like micromark's coverage
  // exclusions would then sit in the hub's static directory, which the repository hygiene scan refuses (WEB-05).
  const output = minify ? {} : { rolldownOptions: { output: { comments: false } } };
  await build({ configFile: join(WEB, 'vite.config.ts'), logLevel, build: { outDir, minify, ...output } });
  const { createHash } = await import('node:crypto');
  return createHash('sha256')
    .update(readFileSync(join(outDir, 'build-manifest.json')))
    .digest('hex');
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const problems = process.argv.includes('--release') ? releaseProblems() : [];
  if (problems.length) {
    console.error(`release build refused:\n  ${problems.join('\n  ')}`);
    process.exit(1);
  }
  const hash = await buildWeb({ logLevel: 'info' });
  if (compiledRootKey() === TEST_ROOT_KEY)
    console.warn('NOTE: the TEST platform root key is compiled in (not a release).');
  console.log(`build hash ${hash}`);
}
