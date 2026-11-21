/// <reference types="vitest/config" />
import { defineConfig } from "vite";

// Everything is bundled and served from this machine: no CDN, no remote fonts, no analytics.
// The dev server listens on 127.0.0.1 only and forwards /api to `room dashboard-api` on 127.0.0.1:8766.
export default defineConfig({
  oxc: { jsx: { runtime: "automatic" } },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8766", changeOrigin: true } },
  },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true },
  build: { sourcemap: false, manifest: true, chunkSizeWarningLimit: 1600 },
  test: { environment: "node", include: ["src/**/*.test.{ts,tsx}"] },
});
