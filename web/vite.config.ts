// Production build (06-web.md section 10): content-hashed assets in src/agent_hub/static/, SRI, build manifest,
// hidden source maps (no reference in the delivered code; used for coverage mapping), no inlined assets (the CSP
// allows no data: fonts), no module-preload polyfill. Run it through `npm run build` (scripts/build.mjs), which sets
// SOURCE_DATE_EPOCH, BUILD_COMMIT and BUILD_VERSION.
import react from '@vitejs/plugin-react';
import { dirname, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, type UserConfig } from 'vite';
import { noLibraryGlobals } from './build/globals.ts';
import { chunkGroups } from './build/chunks.ts';
import { buildManifest, compiledRootKey, rootKeyFingerprint } from './build/manifest.ts';
import { lazyIntegrity, sri } from './build/sri.ts';

export const WEB_DIR = fileURLToPath(new URL('.', import.meta.url));
export const OUT_DIR = fileURLToPath(new URL('../src/agent_hub/static', import.meta.url));

/** Path of a source map entry relative to web/ (`src/main.tsx`), whatever the output directory. */
const fromWeb = (source: string, map: string): string => relative(WEB_DIR, resolve(dirname(map), source));

export default defineConfig((): UserConfig => {
  const rootKey = compiledRootKey();
  rootKeyFingerprint(rootKey);
  return {
    envDir: false,
    define: { 'import.meta.env.VITE_PLATFORM_ROOT_KEY': JSON.stringify(rootKey) },
    root: WEB_DIR,
    base: '/',
    // public/ holds only agents.md, the guide for AI agents (a byte-identical copy of docs/platform/agents.md, ADR-056
    // section 8), delivered by the hub as `/agents.md`.
    publicDir: 'public',
    plugins: [
      lazyIntegrity(),
      react(),
      noLibraryGlobals(),
      sri(),
      buildManifest({
        rootKey,
        version: process.env.BUILD_VERSION ?? '0.0.0',
        commit: process.env.BUILD_COMMIT ?? 'unknown',
      }),
    ],
    build: {
      outDir: OUT_DIR,
      emptyOutDir: true,
      assetsDir: 'assets',
      assetsInlineLimit: 0,
      sourcemap: 'hidden',
      modulePreload: { polyfill: false },
      target: 'es2022',
      reportCompressedSize: false,
      rolldownOptions: {
        output: {
          // Few, larger chunks for the first load, assigned by the path relative to web/ (build/chunks.ts).
          codeSplitting: { groups: chunkGroups(WEB_DIR) },
          // Map sources relative to web/ (`src/main.tsx`), independent of the output location.
          sourcemapPathTransform: (source: string, map: string) => fromWeb(source, map),
          // Library sources are marked for debuggers; decided on the same location-independent path.
          sourcemapIgnoreList: (source: string, map: string) => fromWeb(source, map).startsWith('node_modules/'),
        },
      },
    },
  };
});
