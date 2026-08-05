import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Not "/app/", even though the shell is mounted there during the migration.
  // Assets are served from /assets by their own mount, so moving the shell to /
  // at the end is a routing change with no rebuild.
  base: "/",
  build: {
    // Inside the package so FastAPI finds it via HERE, and so it lands in a
    // wheel if one is ever built. node_modules stays out here at the root.
    outDir: "../jobhunt/web/dist",
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    strictPort: true,
    // `make dev` on 8000 owns the data; this owns the bundle. localhost is a
    // secure context, so navigator.clipboard works in dev too — the Fill
    // helper's copy buttons are testable without Tailscale.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
