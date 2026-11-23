// No globals on `window` in the production bundle (06-web.md section 13). React Router 6 announces its version to
// developer tools with `window.__reactRouterVersion = …`; this plugin removes exactly that statement from the
// library module. `npm run build:check` and the Playwright suite verify that no global remains.
import type { Plugin } from 'vite';

export const LIBRARY_GLOBALS: readonly (readonly [RegExp, string])[] = [
  [/window\.__reactRouterVersion\s*=\s*REACT_ROUTER_VERSION;/g, 'void REACT_ROUTER_VERSION;'],
];

export function stripGlobals(code: string): string {
  return LIBRARY_GLOBALS.reduce((text, [pattern, replacement]) => text.replace(pattern, replacement), code);
}

export function noLibraryGlobals(): Plugin {
  return {
    name: 'agent-platform-no-library-globals',
    apply: 'build',
    transform(code, id) {
      if (!id.includes('/node_modules/react-router')) return null;
      const stripped = stripGlobals(code);
      return stripped === code ? null : { code: stripped, map: null };
    },
  };
}
