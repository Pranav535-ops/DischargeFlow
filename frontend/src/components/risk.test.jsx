import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { queryClient } from '@/lib/queries';
import { riskInputsBody, riskLabel } from '@/lib/plans';

vi.mock('@/context/AuthContext', () => ({ useAuth: () => ({ user: { user_id: 'user_n', role: 'nurse', name: 'Nurse Test' } }) }));
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { user_id: 'user_n', role: 'nurse', name: 'Nurse Test' } }) }));

beforeEach(() => {
  vi.restoreAllMocks();
  queryClient.clear();
  ehr = { available: false, reason: 'no_launch', detail: 'Open this patient from the EHR.' };
});

const risk = {
  score: 14, max_score: 19, high_risk: true, threshold: 10, complete: true, missing: [], version: 'lace-2010.1',
  method: 'LACE index', citation: 'van Walraven C, et al. CMAJ 2010;182(6):551-557. doi:10.1503/cmaj.091117',
  components: [
    { key: 'length_of_stay', letter: 'L', label: 'Length of stay', points: 4, max_points: 7,
      explanation: '5 days to the expected discharge date' },
    { key: 'acuity', letter: 'A', label: 'Acuity of admission', points: 3, max_points: 3,
      explanation: 'Emergency admission: unplanned (acute)' },
    { key: 'comorbidity', letter: 'C', label: 'Comorbidity (Charlson index)', points: 5, max_points: 5,
      explanation: 'Charlson index 5: Congestive heart failure 2, Dementia 3' },
    { key: 'ed_visits', letter: 'E', label: 'Emergency visits in the 6 months before admission', points: 2,
      max_points: 4, explanation: "2 visits, recorded by staff from the hospital's records" },
  ],
  inputs: { admission_type: 'E', patient_class: null, comorbidities: ['congestive_heart_failure', 'dementia'],
    ed_visits_recorded: 2, ed_visits_counted: 0 },
  suggested_tasks: [
    { key: 'follow_up_7_days', title: 'Follow-up Appointment Within 7 Days', assigned_role: 'nurse', added: true },
    { key: 'post_discharge_call', title: 'Post-discharge Phone Call Within 72 Hours', assigned_role: 'nurse',
      added: false },
  ],
};

const codes = {
  dispositions: ['home'], delay_codes: ['placement'], roles: ['nurse'],
  admission_types: [{ code: 'E', label: 'Emergency', acute: true }, { code: 'C', label: 'Elective', acute: false }],
  comorbidities: [{ key: 'congestive_heart_failure', label: 'Congestive heart failure', weight: 2 },
    { key: 'dementia', label: 'Dementia', weight: 3 }, { key: 'aids', label: 'AIDS', weight: 4 }],
  readmission_risk: { threshold: 10 },
};

const mockApi = () => vi.spyOn(api, 'get').mockImplementation(async (url) => {
  if (url === '/patients/pat_1/encounters') return { data: [{ encounter_id: 'enc_1', status: 'active' }] };
  if (url === '/encounters/enc_1/plan') {
    return { data: { encounter_id: 'enc_1', expected_discharge_date: '2026-10-22', planned_disposition: 'home',
      reviewed_today: true, delays: [], readmission_risk: risk } };
  }
  if (url === '/discharge-plans/codes') return { data: codes };
  if (url === '/downtime/status') return { data: { read_only: false } };
  if (url === '/smart/clinical-context/pat_1') return { data: ehr };
  throw new Error(`unexpected ${url}`);
});

let ehr = { available: false, reason: 'no_launch', detail: 'Open this patient from the EHR.' };
const ehrWithProposals = {
  available: true, medications: [], allergies: [], errors: {},
  problems: [{ name: 'Chronic systolic heart failure' }, { name: 'COPD' }],
  charlson_citation: 'Quan H, et al. Med Care 2005;43(11):1130-1139.',
  charlson_proposals: [
    { key: 'congestive_heart_failure', label: 'Congestive heart failure', weight: 2,
      sources: [{ condition: 'Chronic systolic heart failure', system: 'ICD-10-CM', code: 'I50.22' }] },
    { key: 'aids', label: 'AIDS', weight: 4, sources: [{ condition: 'AIDS', system: 'SNOMED CT', code: '62479008' }] },
  ],
};

describe('Readmission risk on the discharge plan', () => {
  it('explains each LACE component, offers the suggested tasks and records inputs', async () => {
    const { default: DischargePlanCard } = await import('./DischargePlanCard');
    mockApi();
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { added: [] } });
    const put = vi.spyOn(api, 'put').mockResolvedValue({ data: {} });
    render(<QueryClientProvider client={queryClient}><DischargePlanCard patientId="pat_1" /></QueryClientProvider>);

    expect(await screen.findByTestId('risk-score')).toHaveTextContent('14 / 19');
    expect(screen.getByTestId('risk-level')).toHaveTextContent('High risk');
    const parts = screen.getByTestId('risk-components');
    expect(within(parts).getByText(/Charlson index 5: Congestive heart failure 2, Dementia 3/)).toBeInTheDocument();
    expect(within(parts).getByText(/5 days to the expected discharge date/)).toBeInTheDocument();
    expect(screen.getByText(/not an AI prediction/)).toBeInTheDocument();

    // Suggested, not forced: the one already added shows so; the other is a button.
    const suggestions = screen.getByTestId('risk-suggestions');
    expect(within(suggestions).getByText('Added')).toBeInTheDocument();
    expect(screen.queryByTestId('risk-add-follow_up_7_days')).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId('risk-add-post_discharge_call'));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/encounters/enc_1/risk-tasks', { keys: ['post_discharge_call'] }));

    await userEvent.click(screen.getByTestId('risk-edit'));
    await userEvent.click(await screen.findByTestId('risk-comorbidity-aids'));
    await userEvent.selectOptions(screen.getByTestId('risk-admission-type'), 'C');
    await userEvent.click(screen.getByTestId('risk-save'));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/encounters/enc_1/risk-inputs', {
      admission_type: 'C', comorbidities: ['aids', 'congestive_heart_failure', 'dementia'] }));
  });

  it('proposes Charlson flags from the EHR problem list, unchecked until the clinician confirms and saves', async () => {
    ehr = ehrWithProposals;
    const { default: DischargePlanCard } = await import('./DischargePlanCard');
    mockApi();
    const put = vi.spyOn(api, 'put').mockResolvedValue({ data: {} });
    render(<QueryClientProvider client={queryClient}><DischargePlanCard patientId="pat_1" /></QueryClientProvider>);

    const proposals = await screen.findByTestId('risk-proposals');
    expect(within(proposals).getByText(/not counted until a clinician confirms/)).toBeInTheDocument();
    // Each proposal names the condition and code it came from.
    const chf = screen.getByTestId('risk-proposal-congestive_heart_failure');
    expect(chf).toHaveTextContent('from Chronic systolic heart failure (ICD-10-CM I50.22)');
    expect(within(chf).getByText('Recorded')).toBeInTheDocument();  // already on the plan: nothing to accept
    expect(screen.queryByTestId('risk-proposal-accept-congestive_heart_failure')).not.toBeInTheDocument();
    const accept = screen.getByTestId('risk-proposal-accept-aids');
    expect(accept).not.toBeChecked();
    expect(screen.getByTestId('risk-proposal-aids')).toHaveTextContent('from AIDS (SNOMED CT 62479008)');
    expect(screen.getByTestId('risk-score')).toHaveTextContent('14 / 19');  // the score ignores proposals

    // Ticking opens the form with the flag set; nothing is saved until the clinician saves.
    await userEvent.click(accept);
    expect(accept).toBeChecked();
    expect(await screen.findByTestId('risk-comorbidity-aids')).toBeChecked();
    expect(screen.getByTestId('risk-form')).toHaveTextContent('proposed by the EHR');
    expect(put).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId('risk-save'));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/encounters/enc_1/risk-inputs', {
      comorbidities: ['aids', 'congestive_heart_failure', 'dementia'] }));
  });

  it('cancelling drops an accepted proposal, and no EHR launch means no proposals', async () => {
    ehr = ehrWithProposals;
    const { default: DischargePlanCard } = await import('./DischargePlanCard');
    mockApi();
    const put = vi.spyOn(api, 'put').mockResolvedValue({ data: {} });
    const { unmount } = render(
      <QueryClientProvider client={queryClient}><DischargePlanCard patientId="pat_1" /></QueryClientProvider>);
    await userEvent.click(await screen.findByTestId('risk-proposal-accept-aids'));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByTestId('risk-proposal-accept-aids')).not.toBeChecked();
    expect(put).not.toHaveBeenCalled();
    unmount();

    ehr = { available: false, reason: 'no_launch', detail: 'Open this patient from the EHR.' };
    queryClient.clear();
    render(<QueryClientProvider client={queryClient}><DischargePlanCard patientId="pat_1" /></QueryClientProvider>);
    expect(await screen.findByTestId('risk-score')).toBeInTheDocument();
    expect(screen.queryByTestId('risk-proposals')).not.toBeInTheDocument();
  });

  it('marks a partial score', async () => {
    const { default: ReadmissionRiskPanel } = await import('./ReadmissionRiskPanel');
    vi.spyOn(api, 'get').mockResolvedValue({ data: codes });
    render(
      <QueryClientProvider client={queryClient}>
        <ReadmissionRiskPanel encounterId="enc_1" canEdit={false}
          risk={{ ...risk, score: 2, high_risk: false, complete: false, missing: ['acuity'], suggested_tasks: [] }} />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId('risk-partial')).toBeInTheDocument();
    expect(screen.getByTestId('risk-level')).toHaveTextContent('Below 10');
    expect(screen.queryByTestId('risk-edit')).not.toBeInTheDocument();
  });
});

describe('risk helpers', () => {
  it('labels a score for the review list', () => {
    expect(riskLabel(null)).toBeNull();
    expect(riskLabel({ score: 14, high_risk: true, complete: true })).toBe('LACE 14 high');
    expect(riskLabel({ score: 3, high_risk: false, complete: false })).toBe('LACE 3 (partial)');
  });

  it('sends only what changed, and records "none" as an empty list', () => {
    const inputs = { admission_type: 'E', comorbidities: ['dementia'], ed_visits_recorded: null };
    expect(riskInputsBody(inputs, { admission_type: 'E', comorbidities: ['dementia'], ed_visits_recorded: '' })).toBeNull();
    expect(riskInputsBody(inputs, { admission_type: '', comorbidities: ['dementia'], ed_visits_recorded: '3' }))
      .toEqual({ admission_type: null, ed_visits_recorded: 3 });
    expect(riskInputsBody({ comorbidities: null }, { admission_type: '', comorbidities: [], ed_visits_recorded: '' }))
      .toEqual({ comorbidities: [] });
  });
});
