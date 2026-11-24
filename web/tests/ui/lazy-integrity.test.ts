// KOR-04 (AUD-01 A-08): the page's preload helper reads the asset table of the real document (jsdom).
import { expect, it } from 'vitest';

const importer = (spec: string, value: string) =>
  new Function(
    `return () => { return Promise.resolve(${JSON.stringify(value)}); import(${JSON.stringify(spec)}); }`,
  )() as () => Promise<string>;
import { __vitePreload, INTEGRITY_META } from '../../src/core/assets/integrity';

it('uses the document of the page', async () => {
  const meta = document.createElement('meta');
  meta.name = INTEGRITY_META;
  meta.content = '/assets/x.js sha384-X';
  document.head.append(meta);
  const loading = __vitePreload(importer('./x.js', 'loaded'), ['assets/x.js']);
  const link = document.head.querySelector<HTMLLinkElement>('link[rel="modulepreload"]')!;
  expect([link.getAttribute('integrity'), link.crossOrigin, link.href]).toEqual([
    'sha384-X',
    'anonymous',
    new URL('/assets/x.js', document.baseURI).href,
  ]);
  link.dispatchEvent(new Event('load'));
  expect(await loading).toBe('loaded');
  await expect(__vitePreload(importer('./y.js', 'never'))).rejects.toThrow('no integrity');
});
