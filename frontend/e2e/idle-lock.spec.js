// Shared-workstation safety (ADR-002 item 4): after five minutes without input the app locks, patient data leaves
// the page, and the refresh session is revoked, so reloading or a second person can't resume it.
import { test, expect } from '@playwright/test';
import { open, signInAs } from './helpers';

test('the app locks after five idle minutes and the session cannot be resumed', async ({ page }) => {
  await page.clock.install();
  await signInAs(page, 'nurse');
  await open(page, '/patients');

  await page.clock.fastForward('04:00');
  await page.mouse.move(200, 200);
  await page.clock.fastForward('04:00'); // eight minutes in all, but input at four kept it open
  await expect(page.getByTestId('lock-screen')).toHaveCount(0);

  const revoked = page.waitForRequest(r => r.url().endsWith('/api/auth/logout') && r.method() === 'POST');
  await page.clock.fastForward('05:01');
  await expect(page.getByTestId('lock-screen')).toBeVisible();
  await expect(page.getByTestId('logout-btn')).toHaveCount(0); // the app shell is gone, not just covered
  expect((await revoked).postDataJSON()).toEqual({ reason: 'idle' });

  // The refresh cookie no longer works: reloading lands on the sign-in page, not back in the app.
  await page.reload();
  await expect(page.getByTestId('google-login-btn')).toBeVisible();
});
