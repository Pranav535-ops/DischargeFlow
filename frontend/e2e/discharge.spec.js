// The discharge critical path, across the three clinical roles:
// admit -> initiate discharge -> AI draft (de-identified, provenance shown) -> physician edits and approves ->
// nurse and pharmacist complete their tasks via deep links -> physician completes the discharge.
import { test, expect } from '@playwright/test';
import { completeMyTasks, open, signInAs } from './helpers';

const PATIENT = `E2E Patient ${Date.now()}`;

test('a patient is discharged end to end', async ({ page }) => {
  // --- physician admits the patient ---
  await signInAs(page, 'physician');
  await open(page, '/patients');
  await page.getByTestId('add-patient-btn').click();
  await page.getByTestId('patient-name-input').fill(PATIENT);
  await page.getByTestId('patient-dob-input').fill('1958-03-14');
  await page.getByTestId('patient-admission-input').fill('2026-10-10');
  await page.getByTestId('patient-room-input').fill('4B');
  await page.getByTestId('patient-diagnosis-input').fill('Community-acquired pneumonia');
  await page.getByTestId('patient-physician-input').fill('Dr. E2E');
  await page.getByTestId('submit-patient-btn').click();

  // --- the patient page has its own URL ---
  await page.getByText(PATIENT).click();
  await expect(page).toHaveURL(/\/patients\/pat_[a-z0-9]+$/);
  const patientUrl = page.url();

  await page.getByTestId('initiate-discharge-btn').click();
  await page.getByTestId('view-workflow-btn').click();
  await expect(page).toHaveURL(/\/workflows\/wf_[a-z0-9]+$/);
  const workflowPath = new URL(page.url()).pathname;

  // --- AI draft from clinical notes, then edit and approve ---
  await page.getByTestId('clinical-notes-input').fill(
    `Course: ${PATIENT} improved on IV ceftriaxone, switched to oral amoxicillin.\n`
    + 'Medications: amoxicillin 875 mg twice daily for 5 days\n'
    + 'Follow-up: Primary care in 7 days',
  );
  await page.getByTestId('generate-summary-btn').click();
  await expect(page.getByTestId('summary-provenance')).toContainText('discharge_summary@v1');
  await expect(page.getByText(/amoxicillin.*875 mg twice daily for 5 days/).first()).toBeVisible();
  await expect(page.getByTestId('grounding-warnings')).toHaveCount(0); // nothing invented

  await page.getByTestId('edit-summary-btn').click();
  const editor = page.getByTestId('summary-editor');
  await expect(editor).toHaveValue(new RegExp(PATIENT)); // re-identified for the physician
  await editor.fill((await editor.inputValue()).replace('Primary care in 7 days', 'Primary care in 5 days'));
  await page.getByTestId('approve-summary-btn').click();
  await expect(page.getByTestId('summary-signed')).toBeVisible();
  await expect(page.getByText(/Approved with physician edits/)).toBeVisible();
  await expect(page.getByTestId('signed-version')).toContainText('Version 1 signed by');
  await expect(page.getByTestId('summary-editor')).toHaveCount(0); // signed text can't be edited

  // --- nurse and pharmacist finish their tasks, arriving by deep link ---
  for (const role of ['nurse', 'pharmacist']) {
    await signInAs(page, role);
    await open(page, workflowPath);
    await completeMyTasks(page);
  }

  // --- physician completes the discharge ---
  await signInAs(page, 'physician');
  await open(page, workflowPath);
  await page.getByTestId('complete-workflow-btn').click();
  await expect(page.getByText('Completed', { exact: true }).first()).toBeVisible();

  // After discharge the signed summary takes addenda, never edits.
  await page.getByTestId('addendum-input').fill('Addendum: final culture negative.');
  await page.getByTestId('add-addendum-btn').click();
  await expect(page.getByTestId('addenda')).toContainText('final culture negative');
  await expect(page.getByTestId('version-history')).toContainText('Addendum 1 signed by');

  // Back and reload keep working on the patient's own URL.
  await page.goto(patientUrl);
  await expect(page.getByRole('combobox')).toHaveValue('discharged');
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`${workflowPath}$`));
});

test('the FHIR API exposes the approved summary', async ({ page }) => {
  await signInAs(page, 'physician');
  const refresh = await page.request.post('/api/auth/refresh');
  const { token } = await refresh.json();
  const docs = await page.request.get('/api/fhir/R4/DocumentReference?_count=50', {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(docs.headers()['content-type']).toContain('application/fhir+json');
  const bundle = await docs.json();
  expect(bundle.resourceType).toBe('Bundle');
  const texts = bundle.entry.map(e => Buffer.from(e.resource.content[0].attachment.data, 'base64').toString('utf-8'));
  expect(texts.some(t => t.includes('Primary care in 5 days'))).toBeTruthy(); // the physician's approved edit
  // The addendum follows the signed version as a further attachment, and the document is marked amended.
  const amended = bundle.entry.map(e => e.resource).find(r => r.docStatus === 'amended');
  expect(amended.content).toHaveLength(2);
  expect(Buffer.from(amended.content[1].attachment.data, 'base64').toString('utf-8')).toContain('final culture negative');
});
