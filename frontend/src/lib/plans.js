// Discharge plan helpers (ADR-002 items 8 and 10), mirroring discharge/planning.py. The API is the authority; these
// decide which controls to show and when to ask for a delay reason.
import { askDelayReason } from '@/lib/tasks';

export const DISPOSITION_LABELS = {
  home: 'Home',
  home_services: 'Home with services',
  snf: 'Skilled nursing facility',
  rehab: 'Inpatient rehab',
  hospice: 'Hospice',
  ama: 'Against medical advice',
  transfer: 'Transfer to another hospital',
  expired: 'Expired',
};

// plan:manage is seeded for physicians and nurses (admin holds every permission).
export const canManagePlan = (user) => ['physician', 'nurse', 'admin'].includes(user?.role);

// A plan's date ('YYYY-MM-DD') shown as a local calendar date, not shifted by the time zone.
export const formatPlanDate = (value) => (value ? new Date(`${value}T00:00`).toLocaleDateString() : null);

// The PUT /encounters/:id/plan body from the edit form ({expected_discharge_date, planned_disposition}, '' = none).
// Moving the date later asks for a delay reason. Returns null if nothing changed or the user cancels.
export const planUpdateBody = (plan, form, ask = window.prompt) => {
  const body = {};
  const date = form.expected_discharge_date || null;
  const disposition = form.planned_disposition || null;
  if (date !== (plan?.expected_discharge_date ?? null)) body.expected_discharge_date = date;
  if (disposition !== (plan?.planned_disposition ?? null)) body.planned_disposition = disposition;
  if (!Object.keys(body).length) return null;
  const old = plan?.expected_discharge_date;
  if (body.expected_discharge_date && old && body.expected_discharge_date > old) {
    const delay = askDelayReason('The expected discharge date is moving later.', ask);
    if (!delay) return null;
    Object.assign(body, delay);
  }
  return body;
};

// Readmission risk (ADR-002 item 20): the LACE score, computed by the API (app/risk). A short label for lists.
export const riskLabel = (risk) => {
  if (!risk) return null;
  return `LACE ${risk.score}${risk.high_risk ? ' high' : ''}${risk.complete ? '' : ' (partial)'}`;
};

// The PUT /encounters/:id/risk-inputs body from the edit form ({admission_type: '' | code, comorbidities: [keys],
// ed_visits_recorded: '' | number string}). Only what changed; null for a cleared value. Null if nothing changed.
export const riskInputsBody = (inputs, form) => {
  const body = {};
  const admissionType = form.admission_type || null;
  if (admissionType !== (inputs?.admission_type ?? null)) body.admission_type = admissionType;
  const before = [...(inputs?.comorbidities ?? [])].sort();
  const after = [...form.comorbidities].sort();
  // Saving the form records the comorbidities, even "none of them": an empty list is different from not recorded.
  if (inputs?.comorbidities == null || before.join() !== after.join()) body.comorbidities = after;
  const visits = form.ed_visits_recorded === '' ? null : Number(form.ed_visits_recorded);
  if (visits !== (inputs?.ed_visits_recorded ?? null)) body.ed_visits_recorded = visits;
  return Object.keys(body).length ? body : null;
};

// The POST /encounters/:id/plan/review body: a delay reason on the first review of a day after the expected date
// has passed. Returns null if the user cancels.
export const planReviewBody = (plan, ask = window.prompt) => {
  if (!plan?.expected_date_passed || plan.reviewed_today) return {};
  return askDelayReason('The expected discharge date has passed and the patient is still in.', ask);
};
