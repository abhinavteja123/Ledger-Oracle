import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Builds into ../static/dist, which app.py mounts at /static and serves
// index.html from for every app route (client-side routing via react-router).
// `base: "/static/"` so the built asset URLs (/static/assets/*.js) resolve
// correctly regardless of which app route index.html was served from.
export default defineConfig({
  plugins: [react()],
  base: "/static/",
  build: {
    outDir: "../static/dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/verify": "http://127.0.0.1:8000",
      "/review": "http://127.0.0.1:8000",
      "/admin": "http://127.0.0.1:8000",
    },
  },
});
