// Subresource integrity for lazily loaded chunks (04-crypto.md section 9, 06-web.md section 10.2; KOR-04, AUD-01 A-08).
//
// The build (`web/build/sri.ts`) writes the SHA-384 of every script and stylesheet under `/assets/` into
// `<meta name="asset-integrity" content="<path> <sha384-…> …">` of index.html (index.html is the anchor that the
// published build hashes cover; an inline import map would need `script-src` to allow inline code) and replaces the
// bundler's preload helper (`__vitePreload`, which every dynamic `import()` goes through with the list of files the
// chunk needs: the chunk itself, its static imports and its CSS) by this module. Before the chunk is imported, every
// file of the list is loaded through `<link rel="modulepreload"|"stylesheet" integrity crossorigin>` and awaited: the
// browser puts a module fetched by a module preload into its module map, so the following `import()` (and the static
// imports inside the chunk) run exactly the checked bytes. A file without a known hash, or whose bytes do not match,
// rejects the import – nothing of it runs (fail closed).
//
// The chunk itself is always checked, whatever the list says: the bundler leaves the list empty for a chunk that
// needs nothing but itself, so the helper reads the target from the import function's own source text
// (`() => import("./X.js")`, resolved against `/assets/` where all chunks live); a function whose target it cannot
// read rejects. One promise per loaded file and page; a failed file is tried (and checked) again on the next import –
// a browser keeps a failed module as failed until the page reloads, a stylesheet can recover.

/** The parts of `document` the loader needs (tests pass their own). */
export interface AssetDocument {
  readonly baseURI: string;
  readonly head: { appendChild(node: unknown): unknown };
  querySelector(selector: string): { getAttribute(name: string): string | null } | null;
  createElement(tag: 'link'): AssetLink;
}

export interface AssetLink {
  rel: string;
  crossOrigin: string | null;
  href: string;
  setAttribute(name: 'integrity', value: string): void;
  addEventListener(type: 'load' | 'error', listener: () => void, options: { once: true }): void;
}

export const INTEGRITY_META = 'asset-integrity';

/** The loader's refusal of a chunk: unknown hash, refused bytes or a failed fetch (the browser does not tell them apart). */
export class ChunkUnavailable extends Error {
  override readonly name = 'ChunkUnavailable';
}

const DYNAMIC_IMPORT_FAILED = /dynamically imported module/i;

/** Whether an error is a chunk that could not be loaded: the loader's refusal or the browser's failed dynamic import
 * (a module that failed once stays failed until the page reloads). Everything else is a bug of the loaded code. */
export function isChunkUnavailable(error: unknown): boolean {
  return error instanceof ChunkUnavailable || (error instanceof TypeError && DYNAMIC_IMPORT_FAILED.test(error.message));
}

/** `path → hash` of the meta element, keyed by absolute URL. */
export function integrityTable(doc: AssetDocument): Map<string, string> {
  const words = (doc.querySelector(`meta[name="${INTEGRITY_META}"]`)?.getAttribute('content') ?? '')
    .split(/\s+/)
    .filter((word) => word !== '');
  const table = new Map<string, string>();
  for (let index = 0; index + 1 < words.length; index += 2) {
    table.set(new URL(words[index]!, doc.baseURI).href, words[index + 1]!);
  }
  return table;
}

export type Preload = <T>(baseModule: () => Promise<T>, deps?: readonly string[]) => Promise<T>;

/** Where the build puts every chunk (`build.assetsDir`); relative imports between chunks resolve against it. */
export const ASSET_DIR = '/assets/';
const IMPORT_RE = /\bimport\(\s*(["'`])(\.\/[^"'`]+)\1\s*\)/;

/** The chunk a bundler-generated import function loads (absolute URL), or null when it cannot be read. */
export function importTarget(baseModule: () => unknown, baseURI: string): string | null {
  const match = IMPORT_RE.exec(Function.prototype.toString.call(baseModule));
  return match === null ? null : new URL(match[2]!, new URL(ASSET_DIR, baseURI)).href;
}

/** The preload helper over `doc` (module comment). */
export function preloadWith(doc: AssetDocument): Preload {
  let table: Map<string, string> | undefined;
  const loaded = new Map<string, Promise<void>>();
  const load = (href: string): Promise<void> => {
    const known = loaded.get(href);
    if (known !== undefined) return known;
    table ??= integrityTable(doc);
    const integrity = table.get(href);
    const done =
      integrity === undefined
        ? Promise.reject(new ChunkUnavailable(`no integrity for ${href}`))
        : new Promise<void>((resolve, reject) => {
            const link = doc.createElement('link');
            link.rel = new URL(href).pathname.endsWith('.css') ? 'stylesheet' : 'modulepreload';
            link.setAttribute('integrity', integrity);
            link.crossOrigin = 'anonymous';
            link.addEventListener('load', () => resolve(), { once: true });
            link.addEventListener(
              'error',
              () => {
                loaded.delete(href);
                reject(new ChunkUnavailable(`integrity check failed for ${href}`));
              },
              { once: true },
            );
            link.href = href;
            doc.head.appendChild(link);
          });
    loaded.set(href, done);
    return done;
  };
  return (baseModule, deps = []) => {
    const target = importTarget(baseModule, doc.baseURI);
    if (target === null) return Promise.reject(new ChunkUnavailable('cannot tell which chunk the import loads'));
    const files = new Set([target, ...deps.map((dep) => new URL(dep, new URL('/', doc.baseURI)).href)]);
    return Promise.all([...files].map(load)).then(() => baseModule());
  };
}

let pagePreload: Preload | undefined;

/** The helper the bundler's dynamic imports call (the name the bundler expects). */
export const __vitePreload: Preload = (baseModule, deps) =>
  (pagePreload ??= preloadWith(document as unknown as AssetDocument))(baseModule, deps);
