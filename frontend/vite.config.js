import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const root = path.dirname(fileURLToPath(import.meta.url));
// Local API for `npm run dev` / `npm run preview`; the deployed app serves /api from the same origin.
const apiTarget = process.env.VITE_DEV_API ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(root, "src") } },
  // REACT_APP_ kept so existing deployment settings (REACT_APP_BACKEND_URL) keep working after the CRA migration.
  envPrefix: ["VITE_", "REACT_APP_"],
  // "build" (not Vite's default "dist") so vercel.json, the Dockerfile and the deploy workflow are unchanged.
  build: { outDir: "build", sourcemap: false },
  server: { port: 3000, proxy: { "/api": { target: apiTarget, changeOrigin: false } } },
  preview: { port: 4173, proxy: { "/api": { target: apiTarget, changeOrigin: false } } },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.js"],
    include: ["src/**/*.test.{js,jsx}"],
    css: false,
  },
});
