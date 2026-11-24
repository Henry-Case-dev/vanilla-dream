import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// SPA is served by Caddy in production (static files + SPA fallback to index.html).
// In development the API, the stream and the SSE endpoint are proxied to the
// local backend on 127.0.0.1:8100; no domain is hardcoded anywhere in the app.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8100',
        changeOrigin: false,
        // Streaming (SSE) must not be buffered by the dev proxy.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (String(proxyRes.headers['content-type']).includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache';
            }
          });
        },
      },
      '/radio.mp3': { target: 'http://127.0.0.1:8101', changeOrigin: false },
      '/radio.m3u': { target: 'http://127.0.0.1:8100', changeOrigin: false },
      '/radio.pls': { target: 'http://127.0.0.1:8100', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    target: 'es2020',
  },
});
