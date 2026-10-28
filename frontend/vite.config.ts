import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The backend always listens on the loopback address.
const apiTarget = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  server: {
    port: 55173,
    strictPort: true,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: false,
        ws: false,
      },
    },
  },
  preview: {
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: false,
        ws: false,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
    unstubGlobals: true,
  },
});
