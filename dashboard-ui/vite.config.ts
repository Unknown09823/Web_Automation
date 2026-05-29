import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import fs from "node:fs";

/**
 * Vite outputs into the python package's dashboard directory:
 *   - assets land in   src/automation/dashboard/static/assets/...
 *   - index.html lands in src/automation/dashboard/static/index.html, then
 *     a small post-build hook copies it up to src/automation/dashboard/index.html
 *     because that's the path FastAPI's _mount_dashboard reads.
 *
 * Asset URLs are prefixed with /dashboard/static/ so they resolve correctly
 * when mounted at /dashboard/static by FastAPI's StaticFiles handler.
 */
const PKG_DASHBOARD = path.resolve(__dirname, "../src/automation/dashboard");

export default defineConfig({
  base: "/dashboard/static/",
  plugins: [
    react(),
    {
      name: "promote-index-html",
      closeBundle() {
        const fromIndex = path.join(PKG_DASHBOARD, "static", "index.html");
        const toIndex = path.join(PKG_DASHBOARD, "index.html");
        if (fs.existsSync(fromIndex)) {
          fs.copyFileSync(fromIndex, toIndex);
        }
      },
    },
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: path.join(PKG_DASHBOARD, "static"),
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          charts: ["recharts"],
          motion: ["framer-motion"],
          radix: [
            "@radix-ui/react-dialog",
            "@radix-ui/react-dropdown-menu",
            "@radix-ui/react-progress",
            "@radix-ui/react-scroll-area",
            "@radix-ui/react-separator",
            "@radix-ui/react-slot",
            "@radix-ui/react-switch",
            "@radix-ui/react-tabs",
            "@radix-ui/react-tooltip",
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "^/(status|control|plugins|config|tasks|logs|accounts|workflows|metrics|ai|distributed|health)(/.*)?$":
        {
          target: "http://127.0.0.1:8080",
          changeOrigin: true,
        },
    },
  },
});
