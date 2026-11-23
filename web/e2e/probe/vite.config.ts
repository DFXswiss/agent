// Build of the style-injection probe page into e2e/.probe/ (served under /probe/ by the hub test server).
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: '/probe/',
  publicDir: false,
  build: {
    outDir: fileURLToPath(new URL('../.probe', import.meta.url)),
    emptyOutDir: true,
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    target: 'es2022',
    reportCompressedSize: false,
    chunkSizeWarningLimit: 2048,
  },
});
