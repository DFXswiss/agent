import { describe, expect, it } from 'vitest';
import { addIntegrity, htmlProblems, integrityUrl, isDataPreload, parseAttributes, sriHash } from '../../build/sri.ts';

const FILES: Record<string, string> = { '/assets/a.js': 'console.log(1)', '/assets/a.css': 'p{}', '/assets/p.js': 'x' };
const read = (url: string) => new TextEncoder().encode(FILES[url]!);

const VITE_HTML = `<!doctype html><html><head>
<link rel="icon" href="data:," />
<script type="module" crossorigin src="/assets/a.js"></script>
<link rel="modulepreload" crossorigin href="/assets/p.js">
<link rel="stylesheet" crossorigin href="/assets/a.css">
</head><body><div id="root"></div></body></html>`;

describe('SRI', () => {
  it('hashes with SHA-384 in base64', () => {
    expect(sriHash(new TextEncoder().encode('abc'))).toBe(
      'sha384-ywB1P0WjXou1oD1pmsZQBycsMqsO3tFjGotgWkP/W+2AhgcroefMI1i67KE0yCWn',
    );
  });

  it('adds integrity and crossorigin to scripts, module preloads and stylesheets only', () => {
    const html = addIntegrity(VITE_HTML, read);
    expect(html).toContain(`<script type="module" src="/assets/a.js" integrity="${sriHash(read('/assets/a.js'))}"`);
    expect(html).toContain(
      `<link rel="modulepreload" href="/assets/p.js" integrity="${sriHash(read('/assets/p.js'))}"`,
    );
    expect(html).toContain(
      `href="/assets/a.css" integrity="${sriHash(read('/assets/a.css'))}" crossorigin="anonymous">`,
    );
    expect(html).toContain('<link rel="icon" href="data:," />');
    expect(htmlProblems(html, read)).toEqual([]);
  });

  it('moves the scripts behind the preloads and stylesheets (KOR-04)', () => {
    const html = addIntegrity(VITE_HTML, read);
    expect(html.indexOf('<script')).toBeGreaterThan(html.indexOf('rel="stylesheet"'));
    expect(html.indexOf('<script')).toBeGreaterThan(html.indexOf('rel="modulepreload"'));
    expect(html).toMatch(/<\/script>\n<\/head>/);
  });

  it('keeps self-closing tags and replaces stale integrity values', () => {
    const html = addIntegrity('<link rel="stylesheet" href="/assets/a.css" integrity="sha384-old" />', read);
    expect(html).toBe(
      `<link rel="stylesheet" href="/assets/a.css" integrity="${sriHash(read('/assets/a.css'))}" crossorigin="anonymous" />`,
    );
  });

  it('leaves external resources alone so that the check reports them', () => {
    const html = '<script src="https://cdn.example/x.js"></script>';
    expect(addIntegrity(html, read)).toBe(html);
    expect(htmlProblems(html, read)).toEqual(['<script> loads https://cdn.example/x.js from outside /assets/']);
  });

  it('reports every CSP violation in index.html', () => {
    const html = `<html><head><style>p{}</style><script>alert(1)</script>
<script src="/assets/a.js"></script><link rel=stylesheet href='/assets/a.css' integrity="sha384-x" crossorigin>
</head><body><div style="color:red" onclick="x()"></div><a href="javascript:void 0">x</a></body></html>`;
    expect(htmlProblems(html, read)).toEqual([
      'inline script',
      'javascript: URL',
      'inline <style> element',
      'script without src',
      '<script> /assets/a.js: integrity missing or wrong',
      '<script> /assets/a.js: crossorigin must be anonymous',
      '<link> /assets/a.css comes after a script (preloads and stylesheets first)',
      '<link> /assets/a.css: integrity missing or wrong',
      '<link> /assets/a.css: crossorigin must be anonymous',
      '<div> with attribute style',
      '<div> with attribute onclick',
    ]);
  });

  it('protects preloads and escapes attribute values', () => {
    const html = addIntegrity(`<link rel="preload" as="script" title='a "b" & c' href="/assets/p.js">`, read);
    expect(html).toBe(
      `<link rel="preload" as="script" title="a &quot;b&quot; &amp; c" href="/assets/p.js" integrity="${sriHash(read('/assets/p.js'))}" crossorigin="anonymous">`,
    );
  });

  it('lets the configuration be preloaded as data from the API only (06-web.md section 12.2)', () => {
    const config = '<link rel="preload" href="/v1/config" as="fetch" crossorigin="anonymous" />';
    expect(addIntegrity(config, read)).toBe(config);
    expect(htmlProblems(config, read)).toEqual([]);
    expect(isDataPreload('link', parseAttributes('rel="preload" as="fetch"'))).toBe(true);
    expect(isDataPreload('link', parseAttributes('rel="preload" as="script"'))).toBe(false);
    expect(isDataPreload('script', parseAttributes('rel="preload" as="fetch"'))).toBe(false);
    expect(htmlProblems('<link rel="preload" as="fetch" href="https://evil.example/x">', read)).toEqual([
      '<link> preloads https://evil.example/x outside /v1/',
    ]);
    expect(htmlProblems('<link rel="preload" as="fetch">', read)).toEqual(['<link> preloads  outside /v1/']);
  });

  it('parses attribute spellings', () => {
    expect([...parseAttributes(` a="1" b='2' c=3 D`)]).toEqual([
      ['a', '1'],
      ['b', '2'],
      ['c', '3'],
      ['d', ''],
    ]);
    expect(integrityUrl('link', new Map([['rel', 'icon']]))).toBeNull();
    expect(integrityUrl('link', new Map())).toBeNull();
    expect(integrityUrl('link', new Map([['rel', 'stylesheet']]))).toBeNull();
    expect(integrityUrl('script', new Map())).toBeNull();
    expect(integrityUrl('div', new Map([['src', 'x']]))).toBeNull();
  });
});
