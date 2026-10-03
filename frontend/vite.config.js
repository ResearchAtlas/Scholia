// The interface's build: React, the @ alias of the current app's stack, and the record of
// the npm packages the bundle contains (licenses.mjs). For development, run the backend with
// tools/dev.py and open the dev server; /api goes to the backend.
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { licenses } from './licenses.mjs';

export default defineConfig({
  plugins: [react(), licenses()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: { '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false } },
  },
  build: { sourcemap: false, chunkSizeWarningLimit: 1500 },
});
