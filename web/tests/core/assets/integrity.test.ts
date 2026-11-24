// KOR-04 (AUD-01 A-08): the preload helper of lazily loaded chunks – every file of a dynamic import is loaded through a
// link with its integrity and awaited before the import runs; unknown or failing files reject it (fail closed).
import { describe, expect, it } from 'vitest';
import {
  ChunkUnavailable,
  isChunkUnavailable,
  type AssetDocument,
  type AssetLink,
  importTarget,
  INTEGRITY_META,
  integrityTable,
  preloadWith,
} from '../../../src/core/assets/integrity';

class FakeLink implements AssetLink {
  rel = '';
  integrity = '';
  crossOrigin: string | null = null;
  href = '';
  setAttribute(_name: 'integrity', value: string) {
    this.integrity = value;
  }
  readonly listeners = new Map<string, () => void>();
  addEventListener(type: 'load' | 'error', listener: () => void) {
    this.listeners.set(type, listener);
  }
}

function fakeDocument(content: string | null, settle: (link: FakeLink) => 'load' | 'error' | null = () => 'load') {
  const appended: FakeLink[] = [];
  const doc: AssetDocument = {
    baseURI: 'https://hub.example/o/1/t/2',
    head: {
      appendChild(node: unknown) {
        const link = node as FakeLink;
        appended.push(link);
        const outcome = settle(link);
        if (outcome !== null) queueMicrotask(() => link.listeners.get(outcome)!());
        return node;
      },
    },
    querySelector: (selector) =>
      selector === `meta[name="${INTEGRITY_META}"]` && content !== null ? { getAttribute: () => content } : null,
    createElement: () => new FakeLink(),
  };
  return { doc, appended };
}

const TABLE = '/assets/a.js sha384-A /assets/a.css sha384-C  /assets/core.js sha384-K /assets/odd';

/**
 * An import function as the bundler writes it (`() => import("./a.js")`): its source names the chunk, but the import
 * after `return` never runs, so the test controls what it resolves to.
 */
function importer<T>(spec: string, result: () => T): () => Promise<T> {
  const source = `return Promise.resolve().then(result); import(${JSON.stringify(spec)});`;
  return new Function('result', `return () => { ${source} }`)(result) as () => Promise<T>;
}

describe('importTarget', () => {
  it('reads the chunk of an import function, relative to the asset directory', () => {
    const base = 'https://hub.example/o/1';
    expect(
      importTarget(
        importer('./a.js', () => 1),
        base,
      ),
    ).toBe('https://hub.example/assets/a.js');
    expect(importTarget(() => import(/* not literal */ `${'x'}`), base)).toBeNull();
    expect(importTarget(async () => 1, base)).toBeNull();
  });
});

describe('integrityTable', () => {
  it('reads the pairs of the meta element as absolute URLs', () => {
    expect([...integrityTable(fakeDocument(TABLE).doc)]).toEqual([
      ['https://hub.example/assets/a.js', 'sha384-A'],
      ['https://hub.example/assets/a.css', 'sha384-C'],
      ['https://hub.example/assets/core.js', 'sha384-K'],
    ]);
    expect(integrityTable(fakeDocument(null).doc).size).toBe(0);
    expect(
      integrityTable({ ...fakeDocument(null).doc, querySelector: () => ({ getAttribute: () => null }) }).size,
    ).toBe(0);
  });
});

describe('preloadWith', () => {
  it('loads every file with its integrity before the import, once per file', async () => {
    const { doc, appended } = fakeDocument(TABLE);
    const preload = preloadWith(doc);
    let imported = 0;
    const load = importer('./a.js', () => {
      expect(appended.every((link) => link.listeners.size === 2)).toBe(true);
      return ++imported;
    });
    expect(await preload(load, ['assets/a.js', 'assets/core.js', 'assets/a.css'])).toBe(1);
    expect(appended.map(({ rel, integrity, crossOrigin, href }) => [rel, integrity, crossOrigin, href])).toEqual([
      ['modulepreload', 'sha384-A', 'anonymous', 'https://hub.example/assets/a.js'],
      ['modulepreload', 'sha384-K', 'anonymous', 'https://hub.example/assets/core.js'],
      ['stylesheet', 'sha384-C', 'anonymous', 'https://hub.example/assets/a.css'],
    ]);
    expect(await preload(load, ['assets/a.js'])).toBe(2);
    expect(await preload(load)).toBe(3);
    expect(appended).toHaveLength(3);
  });

  it('checks the chunk itself even when the list leaves it out, and refuses an unreadable import', async () => {
    const { doc, appended } = fakeDocument(TABLE);
    const preload = preloadWith(doc);
    expect(
      await preload(
        importer('./a.js', () => 'a'),
        [],
      ),
    ).toBe('a');
    expect(appended.map((link) => link.href)).toEqual(['https://hub.example/assets/a.js']);
    await expect(preload(async () => 'never', ['assets/a.js'])).rejects.toThrow(
      'cannot tell which chunk the import loads',
    );
  });

  it('waits for the files and does not import before they loaded', async () => {
    const { doc, appended } = fakeDocument(TABLE, () => null);
    let imported = false;
    const pending = preloadWith(doc)(
      importer('./a.js', () => (imported = true)),
      ['assets/a.js'],
    );
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(imported).toBe(false);
    appended[0]!.listeners.get('load')!();
    await pending;
    expect(imported).toBe(true);
  });

  it('rejects without importing when a file fails its integrity check or has no known hash', async () => {
    const failing = fakeDocument(TABLE, (link) => (link.href.endsWith('a.js') ? 'error' : 'load'));
    const preload = preloadWith(failing.doc);
    let imported = false;
    const load = importer('./a.js', () => (imported = true));
    await expect(preload(load, ['assets/core.js', 'assets/a.js'])).rejects.toThrow(
      'integrity check failed for https://hub.example/assets/a.js',
    );
    // a failed file is checked again on the next import (never loaded without a check)
    await expect(preload(load, ['assets/a.js'])).rejects.toThrow('integrity check failed');
    await expect(preload(importer('./unknown.js', () => 0))).rejects.toThrow(
      'no integrity for https://hub.example/assets/unknown.js',
    );
    await expect(preloadWith(fakeDocument(null).doc)(load, ['assets/a.js'])).rejects.toThrow('no integrity');
    // the refusals carry their own type, so a screen can tell an unavailable chunk from a bug (ACC-FINAL decision 11)
    const typed = preloadWith(fakeDocument(TABLE, (link) => (link.href.endsWith('a.js') ? 'error' : 'load')).doc);
    await expect(typed(load, ['assets/a.js'])).rejects.toBeInstanceOf(ChunkUnavailable);
    await expect(typed(importer('./unknown.js', () => 0))).rejects.toBeInstanceOf(ChunkUnavailable);
    await expect(typed(() => Promise.resolve(0))).rejects.toBeInstanceOf(ChunkUnavailable);
    expect(imported).toBe(false);
    expect(failing.appended.map((link) => link.href)).toEqual([
      'https://hub.example/assets/a.js',
      'https://hub.example/assets/core.js',
      'https://hub.example/assets/a.js',
    ]);
  });
});

describe('isChunkUnavailable', () => {
  it("names the refusals of the loader and the browser's failed dynamic import, nothing else", () => {
    expect(isChunkUnavailable(new ChunkUnavailable('integrity check failed for x'))).toBe(true);
    expect(isChunkUnavailable(new TypeError('Failed to fetch dynamically imported module: https://hub/a.js'))).toBe(
      true,
    );
    expect(isChunkUnavailable(new TypeError('x is not a function'))).toBe(false);
    expect(isChunkUnavailable(new Error('Failed to fetch dynamically imported module'))).toBe(false);
    expect(isChunkUnavailable('integrity check failed')).toBe(false);
  });
});
