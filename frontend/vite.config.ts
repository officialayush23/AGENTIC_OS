import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, the backend runs separately (python -m agentic_os --port 8765 --dev) and Vite proxies /api to it,
// so the browser sees one origin and session cookies just work.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8765", changeOrigin: true },
    },
  },
  build: { target: "es2021", outDir: "dist", sourcemap: false },
  test: { environment: "node" },
} as any);
