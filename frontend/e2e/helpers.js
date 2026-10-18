import { expect } from '@playwright/test';

// Signs the browser context in as a test user of `role` through the dev-only login endpoint, which sets the same
// httpOnly refresh cookie as Google sign-in. The app then restores the session on load like any returning user.
export async function signInAs(page, role) {
  await page.context().clearCookies();
  const resp = await page.request.post('/api/auth/dev-login', { data: { role } });
  expect(resp.ok(), `dev-login failed (${resp.status()}): is ENABLE_DEV_LOGIN set?`).toBeTruthy();
}

export async function open(page, path) {
  await page.goto(path);
  await expect(page.getByTestId('logout-btn')).toBeVisible();
}

// Ticks every task the signed-in role can complete on the workflow page, one at a time (each tick refetches).
export async function completeMyTasks(page) {
  await expect(page.getByRole('heading', { name: 'Discharge Tasks' })).toBeVisible();
  await expect(page.locator('[data-testid^="task-checkbox-"]').first()).toBeVisible(); // tasks have loaded
  const boxes = page.locator('[data-testid^="task-checkbox-"]:not([disabled])');
  for (let remaining = await boxes.count(); remaining > 0; remaining = await boxes.count()) {
    await boxes.first().click();
    await expect(boxes).toHaveCount(remaining - 1);
  }
}
