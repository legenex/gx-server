// Creative Flows island: built into ../web/flows (committed), loaded by web/js/pages/flows.js.
// CSP: script-src 'self', style-src 'self' -> no inline scripts or styles, one CSS file, hashed names,
// no source maps, no module-preload polyfill (it would be an inline helper).
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  base: '/flows/',
  build: {
    outDir: '../web/flows',
    emptyOutDir: true,
    sourcemap: false,
    manifest: 'manifest.json',
    modulePreload: false,
    cssCodeSplit: false,
    target: 'es2022',
    reportCompressedSize: false,
    chunkSizeWarningLimit: 900,
    rolldownOptions: {
      input: { flows: 'src/main.tsx' },
      preserveEntrySignatures: 'exports-only',
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
  test: {
    environment: 'jsdom',
    include: ['test/**/*.test.ts', 'test/**/*.test.tsx'],
    restoreMocks: true,
  },
});
