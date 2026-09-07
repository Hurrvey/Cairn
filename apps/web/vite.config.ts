import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    // The dev server proxies to api-control so the SPA runs same-origin.
    // Same-origin matters here beyond convenience: the session cookie is
    // SameSite=Lax and the CSRF check compares Origin against Host.
    proxy: {
      "/v1": { target: "http://localhost:8000", changeOrigin: false },
      "/healthz": { target: "http://localhost:8000" },
      "/readyz": { target: "http://localhost:8000" },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        // Split the two large, rarely-changing dependencies into their own
        // chunks so an application release does not invalidate them in the
        // browser cache.
        manualChunks: {
          vue: ["vue", "vue-router", "pinia", "vue-i18n"],
          "element-plus": ["element-plus", "@element-plus/icons-vue"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["tests/**/*.spec.ts"],
  },
});
