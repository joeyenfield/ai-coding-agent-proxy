import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The proxy serves the API; in development Vite forwards these paths to it.
const proxyTarget = process.env.AI_PROXY_URL ?? "http://127.0.0.1:9999";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // ws: the shared agent terminals stream over WebSockets under /api/terminals.
      "/api": { target: proxyTarget, ws: true },
      "/health": proxyTarget,
      // The API reference (Swagger UI) and the document it reads.
      "/docs": proxyTarget,
      "/openapi.json": proxyTarget,
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
