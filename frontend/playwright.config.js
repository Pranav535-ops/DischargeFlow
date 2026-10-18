// End-to-end tests (ADR-001 Phase 5, item 18): the real API and the production build, driven in a browser.
//
// Starts the backend (mock AI, dev login enabled, migrations applied) and `vite preview` of a fresh build, which
// proxies /api to the backend exactly as CloudFront/Vercel do in production.
//   E2E_DATABASE_URL  database for the backend (CI: the Postgres service; default: a throwaway SQLite file)
//   E2E_PYTHON        Python with the backend requirements installed (default: python)
//   PW_CHANNEL        e.g. "chrome" to use an installed Chrome instead of Playwright's Chromium
import os from 'node:os';
import path from 'node:path';
import { defineConfig, devices } from '@playwright/test';

const CI = !!process.env.CI;
const database = process.env.E2E_DATABASE_URL
  ?? `sqlite+aiosqlite:///${path.join(os.tmpdir(), `dischargeflow-e2e-${Date.now()}.db`).replaceAll('\\', '/')}`;
const python = process.env.E2E_PYTHON ?? 'python';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false, // the specs share one database and walk one workflow through its states
  workers: 1,
  retries: CI ? 1 : 0,
  timeout: 60_000,
  reporter: CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    ...devices['Desktop Chrome'],
    ...(process.env.PW_CHANNEL ? { channel: process.env.PW_CHANNEL } : {}),
  },
  webServer: [
    {
      command: `${python} -m app.cli migrate && ${python} -m uvicorn server:app --port 8000`,
      cwd: '../backend',
      url: 'http://localhost:8000/api/health',
      reuseExistingServer: !CI,
      timeout: 120_000,
      env: {
        APP_ENV: 'development',
        DATABASE_URL: database,
        JWT_SECRET: 'e2e-only-secret-0123456789-0123456789-abcdef',
        LLM_PROVIDER: 'mock',
        ENABLE_DEV_LOGIN: 'true',
        CORS_ORIGINS: 'http://localhost:4173',
        LOG_FORMAT: 'text',
      },
    },
    {
      command: 'npm run build && npm run preview -- --strictPort',
      url: 'http://localhost:4173',
      reuseExistingServer: !CI,
      timeout: 180_000,
    },
  ],
});
