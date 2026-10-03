import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The proxy serves the API; in development Vite forwards these paths to it.
const proxyTarget = process.env.AI_PROXY_URL ?? "http://127.0.0.1:8181";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": proxyTarget,
      "/health": proxyTarget,
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
