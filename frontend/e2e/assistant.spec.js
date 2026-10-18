// The assistant never writes on its own: a write becomes a proposal that the user confirms or cancels.
import { test, expect } from '@playwright/test';
import { open, signInAs } from './helpers';

test('assistant task proposals need confirmation', async ({ page }) => {
  await signInAs(page, 'nurse');
  const name = `Assist Patient ${Date.now()}`;
  const refresh = await (await page.request.post('/api/auth/refresh')).json();
  const created = await page.request.post('/api/patients', {
    headers: { Authorization: `Bearer ${refresh.token}` },
    data: { name, date_of_birth: '1970-01-01', admission_date: '2026-10-12', room_number: '9',
            diagnosis: 'Cellulitis', attending_physician: 'Dr. E2E' },
  });
  expect(created.ok()).toBeTruthy();

  await open(page, '/assistant');
  const input = page.getByPlaceholder(/Ask me anything/);
  await input.fill(`create a task for ${name} to check the dressing`);
  await input.press('Enter');

  const card = page.getByTestId('assistant-action-card').last();
  await expect(card).toContainText('Needs your confirmation');
  await expect(card).toContainText(name);

  await card.getByTestId('confirm-action-btn').click();
  await expect(card).toContainText('confirmed');
  await expect(page.getByText(/Done: Created task 'Check the dressing'/)).toBeVisible();

  // The confirmed task is real, and visible in the task list.
  await open(page, '/tasks');
  await page.getByTestId('view-all-tasks').click();
  await expect(page.getByText('Check the dressing').first()).toBeVisible();
});
