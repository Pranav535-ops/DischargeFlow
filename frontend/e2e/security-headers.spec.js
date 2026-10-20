// The app is served with the production security headers (vite preview reads them from vercel.json) and every
// screen renders without a Content-Security-Policy violation. Also runs against a deployment (E2E_BASE_URL).
import { test, expect } from '@playwright/test';
import { signInAs } from './helpers';

/* global window, document -- used inside page.addInitScript and page.evaluate, which run in the browser */

const ADMIN_SCREENS = ['/dashboard', '/patients', '/tasks', '/insurance', '/assistant', '/census', '/plans',
  '/capacity', '/analytics', '/deliveries', '/admin', '/insights'];

// Records CSP violations from the moment each document starts, before the app's own scripts run.
async function watchCsp(page) {
  const console = [];
  page.on('console', (msg) => {
    if (/Content Security Policy|Refused to (load|apply|execute|connect|frame)/i.test(msg.text())) console.push(msg.text());
  });
  await page.addInitScript(() => {
    window.__cspViolations = [];
    document.addEventListener('securitypolicyviolation', (e) => {
      window.__cspViolations.push(`${e.violatedDirective} blocked ${e.blockedURI || 'inline'}`);
    });
  });
  return async () => [...console, ...await page.evaluate(() => window.__cspViolations)];
}

test('the app document carries the security headers', async ({ page }) => {
  for (const path of ['/login', '/patients/some-id']) {
    const response = await page.request.get(path);
    const headers = response.headers();
    expect(headers['x-frame-options'], path).toBe('DENY');
    expect(headers['content-security-policy'], path).toContain("frame-ancestors 'none'");
    expect(headers['content-security-policy'], path).toContain("script-src 'self';");
    expect(headers['x-content-type-options'], path).toBe('nosniff');
    expect(headers['referrer-policy'], path).toBe('strict-origin-when-cross-origin');
    expect(headers['permissions-policy'], path).toContain('camera=()');
  }
});

test('the login page renders without CSP violations', async ({ page }) => {
  const violations = await watchCsp(page);
  await page.context().clearCookies();
  await page.goto('/login');
  await expect(page.getByRole('button').first()).toBeVisible();
  await page.waitForLoadState('networkidle');
  expect(await violations()).toEqual([]);
});

test('every admin screen renders without CSP violations', async ({ page }) => {
  const violations = await watchCsp(page);
  await signInAs(page, 'admin');
  for (const path of ADMIN_SCREENS) {
    await page.goto(path);
    await expect(page.getByTestId('logout-btn')).toBeVisible();
    await page.waitForLoadState('load');
    await page.waitForTimeout(750); // lazy chunks, charts, avatars; not networkidle: the notification stream stays open
    expect(await violations(), path).toEqual([]);
  }
});
