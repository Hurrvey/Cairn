import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

const apiProxy = process.env.CAIRN_API_PROXY ?? "http://localhost:8000";

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    // The dev server proxies to api-control so the SPA runs same-origin.
    // Same-origin matters here beyond convenience: the session cookie is
    // SameSite=Lax and the CSRF check compares Origin against Host.
    proxy: {
      "/v1": { target: apiProxy, changeOrigin: false },
      "/healthz": { target: apiProxy },
      "/readyz": { target: apiProxy },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        // Split the large, rarely-changing dependencies into their own chunks
        // so an application release does not invalidate them in the browser
        // cache.
        manualChunks: {
          vue: ["vue", "vue-router", "pinia", "vue-i18n"],
          ui: ["reka-ui", "@vueuse/core", "lucide-vue-next"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["tests/**/*.spec.ts"],
    setupFiles: ["tests/setup.ts"],
    css: false,
  },
});
