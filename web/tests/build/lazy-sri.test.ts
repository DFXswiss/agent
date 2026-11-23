// KOR-04 (AUD-01 A-08): the asset table of index.html and the build checks that every lazily loaded chunk goes
// through the integrity-checking preload helper.
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';
import {
  addAssetIntegrity,
  assetIntegrityProblems,
  checkedImport,
  closingParen,
  dynamicImportProblems,
  INTEGRITY_HELPER,
  lazyIntegrity,
  listAssets,
  PRELOAD_HELPER_ID,
  sriHash,
} from '../../build/sri.ts';

const HELPER = 'Promise.reject(new Error(`cannot tell which chunk the import loads`))';
const MAP =
  'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/Lazy-b.js","assets/lib-c.js","assets/Lazy-b.css"])))=>i.map(i=>d[i]);';
const FILES: Record<string, string> = {
  '/assets/index-a.js': `${MAP}import{x}from"./core-d.js";${HELPER};f(()=>import(\`./Lazy-b.js\`),__vite__mapDeps([0,1,2]))`,
  '/assets/core-d.js': 'export const x=1',
  '/assets/Lazy-b.js': 'import{x}from"./core-d.js";import{c}from"./lib-c.js";export const b=c+x',
  '/assets/lib-c.js': 'export const c=1',
  '/assets/Lazy-b.css': 'p{}',
};
const ASSETS = Object.keys(FILES).sort();
const reader = (files: Record<string, string>) => (url: string) => new TextEncoder().encode(files[url]!);
const read = reader(FILES);
const HTML = '<html><head>\n    <title>t</title>\n  </head><body></body></html>';
const PAGE = '<html><head><script type="module" src="/assets/index-a.js"></script></head></html>';

describe('the asset table', () => {
  it('names every asset with its hash once, in the head, and replaces an older table', () => {
    const html = addAssetIntegrity(HTML, ASSETS, read);
    const table = ASSETS.map((url) => `${url} ${sriHash(read(url))}`).join(' ');
    expect(html).toContain(`    <meta name="asset-integrity" content="${table}" />\n  </head>`);
    expect(assetIntegrityProblems(html, ASSETS, read)).toEqual([]);
    expect(addAssetIntegrity(html, ASSETS, read)).toBe(html);
  });

  it('fails without a table, with two, or with a missing or stale hash', () => {
    expect(assetIntegrityProblems(HTML, ASSETS, read)).toEqual([
      'index.html needs exactly one <meta name="asset-integrity"> (found 0)',
    ]);
    const html = addAssetIntegrity(HTML, ASSETS, read);
    expect(
      assetIntegrityProblems(html.replace('</head>', `${html.match(/<meta[^>]*>/)![0]}</head>`), ASSETS, read),
    ).toHaveLength(1);
    const swapped = HTML.replace(
      '</head>',
      `<meta content="x" name="asset-integrity">${html.match(/<meta[^>]*>/)![0]}</head>`,
    );
    expect(assetIntegrityProblems(swapped, ASSETS, read)).toEqual([
      'index.html needs exactly one <meta name="asset-integrity"> (found 2)',
    ]);
    const partial = addAssetIntegrity(HTML, ASSETS.slice(1), read);
    expect(assetIntegrityProblems(partial, ASSETS, read)).toEqual([
      '<meta name="asset-integrity">: hashes missing or wrong',
    ]);
  });
});

describe('dynamic imports', () => {
  const problems = (index: string, extra: Record<string, string> = {}) =>
    dynamicImportProblems(
      PAGE,
      ASSETS,
      reader({ ...FILES, '/assets/index-a.js': `import{x}from"./core-d.js";${index}`, ...extra }),
    );

  it('pass when every import() goes through the helper with the static imports of its chunk', () => {
    expect(dynamicImportProblems(PAGE, ASSETS, read)).toEqual([]);
    // the chunk itself may be left out (the helper checks it), and a chunk needing nothing new may have no list
    expect(problems(`${MAP}${HELPER};f(()=>import("./Lazy-b.js"),__vite__mapDeps([1]))`)).toEqual([]);
    expect(problems(`${HELPER};f(()=>import('./lib-c.js'),[]);g(()=>import("./lib-c.js"),void 0)`)).toEqual([]);
  });

  it('fail for a missing static import, a bypass, a non-literal import, an unknown chunk or the wrong helper', () => {
    expect(problems(`${MAP}${HELPER};f(()=>import("./Lazy-b.js"),__vite__mapDeps([0]))`)).toEqual([
      '/assets/index-a.js: import() of /assets/Lazy-b.js would load /assets/lib-c.js without integrity',
    ]);
    expect(problems(`${HELPER};f(()=>import("./Lazy-b.js"),[])`)).toEqual([
      '/assets/index-a.js: import() of /assets/Lazy-b.js would load /assets/lib-c.js without integrity',
    ]);
    expect(problems(`${HELPER};import("./lib-c.js");import(x)`)).toEqual([
      '/assets/index-a.js: an import() that does not go through the integrity check',
      '/assets/index-a.js: an import() that does not go through the integrity check',
    ]);
    expect(problems(`${HELPER};f(()=>import("./gone.js"),[])`)).toEqual([
      '/assets/index-a.js: import() of /assets/gone.js, which the build did not write',
    ]);
    expect(problems('f(()=>import("./lib-c.js"),[]);"Unable to preload CSS"')).toEqual([
      'the integrity-checking preload helper is missing (dynamic imports would load unchecked code)',
      "the bundler's own preload helper is in the bundle (it loads chunks without integrity)",
    ]);
  });
});

describe('the import scanner', () => {
  it('reads chained calls and skips parentheses inside strings', () => {
    expect(checkedImport('import(`./S.js`).then(e=>({C:e.S, t:")("})),__vite__mapDeps([4,1])')).toEqual({
      path: './S.js',
      deps: [4, 1],
    });
    expect(checkedImport('import("./S.js") . then ( x ) , void 0')).toEqual({ path: './S.js', deps: [] });
    expect(checkedImport('import("./S.js").then(x),y')).toBeNull();
    expect(checkedImport('import("./S.js").then(x')).toBeNull();
    expect(checkedImport('import(x),[]')).toBeNull();
    expect(closingParen('(")', 0)).toBe(-1);
  });
});

describe('the plugin and the asset list', () => {
  const dir = mkdtempSync(join(tmpdir(), 'lazy-sri-'));
  afterAll(() => rmSync(dir, { recursive: true, force: true }));

  it('lists scripts and stylesheets only, sorted', () => {
    mkdirSync(join(dir, 'assets'));
    for (const name of ['b.js', 'a.css', 'a.js.map', 'x.woff2', 'w.wasm', 'm.mjs'])
      writeFileSync(join(dir, 'assets', name), '');
    expect(listAssets(dir)).toEqual(['/assets/a.css', '/assets/b.js', '/assets/m.mjs', '/assets/w.wasm']);
  });

  it('loads the in-repo helper in place of the bundler helper', () => {
    const plugin = lazyIntegrity();
    const load = plugin.load as (id: string) => string | null;
    expect(plugin).toMatchObject({ name: 'agent-platform-lazy-integrity', apply: 'build', enforce: 'pre' });
    expect(load(PRELOAD_HELPER_ID)).toBe(`export { __vitePreload } from ${JSON.stringify(INTEGRITY_HELPER)};`);
    expect(load('/src/main.tsx')).toBeNull();
    expect(INTEGRITY_HELPER.endsWith(join('src', 'core', 'assets', 'integrity.ts'))).toBe(true);
  });
});
