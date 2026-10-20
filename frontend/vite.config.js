import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const root = path.dirname(fileURLToPath(import.meta.url));
// Local API for `npm run dev` / `npm run preview`; the deployed app serves /api from the same origin.
const apiTarget = process.env.VITE_DEV_API ?? "http://localhost:8000";

// The security headers (CSP, anti-framing, ...) Vercel sends with the app, read from ../vercel.json so `vite preview`,
// and therefore the Playwright E2E run, serves exactly what production does. frontend/nginx.conf carries a copy
// (src/test/securityHeaders.test.js keeps them equal). Only read for preview: the Vercel build may not see the repo root.
function spaSecurityHeaders() {
  const vercel = JSON.parse(fs.readFileSync(path.resolve(root, "../vercel.json"), "utf8"));
  const rule = vercel.headers.find((h) => h.source === "/((?!api/).*)");
  return Object.fromEntries(rule.headers.map(({ key, value }) => [key, value]));
}

export default defineConfig(({ isPreview }) => ({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(root, "src") } },
  // REACT_APP_ kept so existing deployment settings (REACT_APP_BACKEND_URL) keep working after the CRA migration.
  envPrefix: ["VITE_", "REACT_APP_"],
  // "build" (not Vite's default "dist") so vercel.json, the Dockerfile and the deploy workflow are unchanged.
  build: { outDir: "build", sourcemap: false },
  server: { port: 3000, proxy: { "/api": { target: apiTarget, changeOrigin: false } } },
  preview: {
    port: 4173,
    proxy: { "/api": { target: apiTarget, changeOrigin: false } },
    headers: isPreview ? spaSecurityHeaders() : undefined,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.js"],
    include: ["src/**/*.test.{js,jsx}"],
    css: false,
  },
}));
