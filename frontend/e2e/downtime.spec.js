// The downtime procedure (ADR-002 item 6, docs/runbooks/downtime.md): an admin switches read-only mode on, every
// screen shows the banner and refuses changes, the census still prints, and the admin switches it off again.
import { test, expect } from '@playwright/test';
import { open, signInAs } from './helpers';

test('read-only mode during a downtime', async ({ page }) => {
  await signInAs(page, 'admin');
  await open(page, '/admin');
  const card = page.getByTestId('downtime-card');
  await card.getByTestId('downtime-reason').fill('E2E maintenance window');
  page.once('dialog', dialog => dialog.accept());
  await card.getByTestId('start-downtime').click();
  try {
    await expect(page.getByTestId('downtime-banner')).toContainText('E2E maintenance window');

    // A nurse sees the banner everywhere, can't add a patient, and can still read and print the census.
    await signInAs(page, 'nurse');
    await open(page, '/patients');
    await expect(page.getByTestId('downtime-banner')).toBeVisible();
    await expect(page.getByTestId('add-patient-btn')).toBeDisabled();
    await page.getByRole('link', { name: /print census/i }).click();
    await expect(page).toHaveURL(/\/census$/);
    await expect(page.getByRole('heading', { name: /Discharge census: All units/ })).toBeVisible();
  } finally {
    await signInAs(page, 'admin');
    await open(page, '/admin');
    await page.getByTestId('end-downtime').click();
  }
  await expect(page.getByTestId('downtime-banner')).toHaveCount(0);
  await expect(page.getByTestId('downtime-card')).toContainText('Normal');
});
