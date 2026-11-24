import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { describe, expect, it } from 'vitest';
import { noLibraryGlobals, stripGlobals } from '../../build/globals.ts';

type Transform = (code: string, id: string) => { code: string } | null;

describe('library globals', () => {
  it('removes the React Router version global from the pinned library', () => {
    const file = createRequire(import.meta.url).resolve('react-router-dom');
    const source = readFileSync(file.replace(/main\.js$/, 'index.js'), 'utf8');
    expect(source).toContain('window.__reactRouterVersion = REACT_ROUTER_VERSION;');
    expect(stripGlobals(source)).not.toContain('window.__reactRouterVersion');
  });

  it('transforms only React Router modules that contain the statement', () => {
    const transform = noLibraryGlobals().transform as unknown as Transform;
    const code = 'try { window.__reactRouterVersion = REACT_ROUTER_VERSION; } catch {}';
    expect(transform(code, '/x/node_modules/react-router-dom/dist/index.js')).toEqual({
      code: 'try { void REACT_ROUTER_VERSION; } catch {}',
      map: null,
    });
    expect(transform('let a = 1;', '/x/node_modules/react-router/dist/index.js')).toBeNull();
    expect(transform(code, '/x/src/main.tsx')).toBeNull();
  });
});
