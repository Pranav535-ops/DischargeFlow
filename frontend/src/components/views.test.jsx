import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { api } from '@/lib/api';

vi.mock('@/context/AuthContext', () => ({ useAuth: () => ({ user: { role: 'physician', name: 'Dr Test' } }) }));
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { role: 'physician', name: 'Dr Test' } }) }));

const renderAt = (path, routePath, element) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={routePath} element={element} />
          <Route path="*" element={<p>elsewhere</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

beforeEach(() => vi.restoreAllMocks());

describe('WorkflowView (route /workflows/:workflowId)', () => {
  it('loads the workflow from the URL and shows grounding warnings on the draft', async () => {
    const { default: WorkflowView } = await import('./WorkflowView');
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/workflows/wf_1') {
        return { data: {
          workflow_id: 'wf_1', patient_id: 'pat_1', status: 'in_progress', started_at: '2026-10-18T10:00:00Z',
          tasks: [],
          discharge_summary: {
            summary_id: 'sum_1', approved: false, content: 'DISCHARGE SUMMARY', generated_by: 'ai',
            generation: { provider: 'anthropic', model_id: 'claude-sonnet-5-5', prompt_version: 'discharge_summary@v1',
              grounding_warnings: [{ type: 'medication', value: 'warfarin' }] },
          },
        } };
      }
      if (url === '/patients/pat_1') return { data: { patient_id: 'pat_1', name: 'Jane Doe', room_number: '12A' } };
      throw new Error(`unexpected ${url}`);
    });
    renderAt('/workflows/wf_1', '/workflows/:workflowId', <WorkflowView />);

    expect(await screen.findByText(/Jane Doe - Room 12A/)).toBeInTheDocument();
    expect(screen.getByTestId('grounding-warnings')).toHaveTextContent('medication: warfarin');
    expect(screen.getByTestId('summary-provenance')).toHaveTextContent('claude-sonnet-5-5');
  });

  it('sends the physician edits when approving an edited draft', async () => {
    const { default: WorkflowView } = await import('./WorkflowView');
    vi.spyOn(api, 'get').mockImplementation(async (url) => ({ data: url.startsWith('/workflows') ? {
      workflow_id: 'wf_1', patient_id: 'pat_1', status: 'in_progress', started_at: '2026-10-18T10:00:00Z', tasks: [],
      discharge_summary: { summary_id: 'sum_1', approved: false, content: 'Original draft' },
    } : { patient_id: 'pat_1', name: 'Jane Doe', room_number: '1' } }));
    const put = vi.spyOn(api, 'put').mockResolvedValue({ data: {} });
    renderAt('/workflows/wf_1', '/workflows/:workflowId', <WorkflowView />);

    await userEvent.click(await screen.findByTestId('edit-summary-btn'));
    const editor = screen.getByTestId('summary-editor');
    await userEvent.clear(editor);
    await userEvent.type(editor, 'Edited by physician');
    await userEvent.click(screen.getByTestId('approve-summary-btn'));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/ai/approve-summary/sum_1', { content: 'Edited by physician' }));
  });
});

describe('WorkflowView summary lifecycle (ADR-002 item 13)', () => {
  const signedWorkflow = {
    workflow_id: 'wf_1', patient_id: 'pat_1', status: 'completed', started_at: '2026-10-18T10:00:00Z', tasks: [],
    summary_sign_by: '2026-10-19T12:00:00Z', summary_overdue: false,
    discharge_summary: {
      summary_id: 'sum_1', approved: true, signed: true, content: 'Signed text', created_at: '2026-10-18T10:05:00Z',
      versions: [{ version_id: 'sver_1', version: 1, signed_by: 'user_dr', signed_at: '2026-10-18T11:00:00Z' }],
      signed_version: { version_id: 'sver_1', version: 1, signed_by: 'user_dr', signed_at: '2026-10-18T11:00:00Z',
        content: 'Signed text', content_sha256: 'abcdef0123456789', hash_verified: true },
      addenda: [{ addendum_id: 'add_1', version_id: 'sver_1', signed_by: 'user_dr', signed_at: '2026-10-19T09:00:00Z',
        content: 'Late potassium result' }],
      signed_late: false,
    },
  };

  it('shows the signed version, the history and the addenda, and adds a signed addendum', async () => {
    const { default: WorkflowView } = await import('./WorkflowView');
    vi.spyOn(api, 'get').mockImplementation(async (url) => ({ data: url.startsWith('/workflows') ? signedWorkflow
      : url.startsWith('/downtime') ? { read_only: false } : { patient_id: 'pat_1', name: 'Jane Doe', room_number: '1' } }));
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: {} });
    renderAt('/workflows/wf_1', '/workflows/:workflowId', <WorkflowView />);

    expect(await screen.findByTestId('summary-signed')).toHaveTextContent('Signed');
    expect(screen.getByTestId('signed-version')).toHaveTextContent('Version 1 signed by user_dr');
    expect(screen.getByTestId('signed-version')).toHaveTextContent('sha256 abcdef012345');
    expect(screen.getByTestId('version-history')).toHaveTextContent('Addendum 1 signed by user_dr');
    expect(screen.getByTestId('addenda')).toHaveTextContent('Late potassium result');
    expect(screen.queryByTestId('edit-summary-btn')).not.toBeInTheDocument();

    await userEvent.type(screen.getByTestId('addendum-input'), 'Culture negative');
    await userEvent.click(screen.getByTestId('add-addendum-btn'));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/ai/summaries/sum_1/addenda', { content: 'Culture negative' }));
  });

  it('flags an unsigned summary past its sign-by deadline', async () => {
    const { default: WorkflowView } = await import('./WorkflowView');
    vi.spyOn(api, 'get').mockImplementation(async (url) => ({ data: url.startsWith('/workflows') ? {
      ...signedWorkflow, status: 'in_progress', summary_overdue: true,
      discharge_summary: { summary_id: 'sum_1', approved: false, content: 'Draft' },
    } : { patient_id: 'pat_1', name: 'Jane Doe', room_number: '1' } }));
    renderAt('/workflows/wf_1', '/workflows/:workflowId', <WorkflowView />);
    expect(await screen.findByTestId('summary-overdue')).toHaveTextContent('Signature overdue');
    expect(screen.queryByTestId('summary-lifecycle')).not.toBeInTheDocument();
  });
});

describe('PatientDetails (route /patients/:patientId)', () => {
  it('shows a not-found state for an unknown patient id', async () => {
    const { default: PatientDetails } = await import('./PatientDetails');
    vi.spyOn(api, 'get').mockRejectedValue(Object.assign(new Error('404'), { response: { status: 404 } }));
    renderAt('/patients/pat_missing', '/patients/:patientId', <PatientDetails />);
    expect(await screen.findByText('Patient not found.')).toBeInTheDocument();
  });
});

describe('InsightsView', () => {
  it('shows the aggregates and the findings with their cited metrics', async () => {
    const { default: InsightsView } = await import('./InsightsView');
    const aggregates = {
      task_completion_time_by_role: { pharmacist: { n: 6, avg_hours: 20, median_hours: 19 }, nurse: { n: 1 } },
      open_tasks_by_role: { nurse: { n: 4, oldest_hours: 30 } },
      discharge_duration: { n: 0 },
      ai_summaries: { generated: 2, approved: 1, avg_edit_similarity: 0.9, failed_generations: 0 },
    };
    const operations = {
      window_days: 30,
      throughput: { discharges_started: 5, discharges_completed: 4, completed_per_day: {},
        discharge_duration: { n: 4, avg_hours: 30, median_hours: 28 } },
      pending_approval: { wait: { n: 3, avg_hours: 4, median_hours: 4 }, untracked_completed: 1, waiting_now: 2,
        oldest_waiting_hours: null },
      task_sla: { targets_hours: { high: 4, medium: 12, low: 24 }, open_tasks: 3,
        open_breaching_by_role: { pharmacist: { high: 1 } }, completed: { n: 10, breached: 2, breach_rate: 0.2 },
        completed_by_role: {} },
      summary_signing: { sign_by_hours: 24, overdue_unsigned: 2, oldest_overdue_hours: null,
        time_to_sign: { n: 4, avg_hours: 6, median_hours: 5 }, signed: { n: 4, breached: 1 } },
    };
    vi.spyOn(api, 'get').mockImplementation(async (url) => (
      { data: url.startsWith('/analytics/operations') ? operations : aggregates }));
    vi.spyOn(api, 'post').mockResolvedValue({ data: {
      aggregates,
      insights: { headline: 'Pharmacist tasks take longest', findings: [
        { title: 'Pharmacist delay', detail: 'd', suggestion: 's', metric: 'task_completion_time_by_role.pharmacist.avg_hours' },
      ] },
      rejected_citations: ['readmission_rate'],
      generation: { provider: 'mock', model_id: 'mock-1', prompt_version: 'insights@v1' },
    } });
    renderAt('/insights', '/insights', <InsightsView />);

    expect(await screen.findAllByText(/too few to average/)).toHaveLength(2); // nurse n=1, discharges n=0
    const ops = await screen.findByTestId('operations-panel');
    expect(ops).toHaveTextContent('4 completed · 5 started');
    expect(ops).toHaveTextContent('2/10 completed tasks over target (20%)');
    expect(ops).toHaveTextContent('1 older discharge(s) not tracked');
    expect(ops).toHaveTextContent('1 high open, overdue');
    expect(screen.getByTestId('ops-summary-signing')).toHaveTextContent('2 unsigned past deadline');
    expect(screen.getByTestId('ops-summary-signing')).toHaveTextContent('1/4 signed late');
    await userEvent.click(screen.getByTestId('generate-insights-btn'));
    const panel = await screen.findByTestId('insights-panel');
    expect(panel).toHaveTextContent('Pharmacist tasks take longest');
    expect(panel).toHaveTextContent('Based on task_completion_time_by_role.pharmacist.avg_hours');
    expect(panel).toHaveTextContent('1 finding(s) removed');
    expect(panel).toHaveTextContent('offline mock');
  });
});

describe('InsuranceView', () => {
  it('is read-only for roles without claim:manage (physician here)', async () => {
    const { default: InsuranceView } = await import('./InsuranceView');
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/insurance/claims') {
        return { data: [{ claim_id: 'clm_1', patient_id: 'pat_1', insurer_name: 'Acme Health', policy_number: 'P-1',
          billed_amount: 1200, status: 'pending' }] };
      }
      if (url === '/insurance/reports') {
        return { data: { total_claims: 1, total_billed: 1200, total_paid: 0, total_allowed: 0, denial_rate: 0,
          pending_count: 1, accepted_count: 0, denied_count: 0, by_insurer: [] } };
      }
      return { data: [{ patient_id: 'pat_1', name: 'Jane Doe' }] };
    });
    renderAt('/insurance', '/insurance', <InsuranceView />);

    expect(await screen.findByText('Acme Health')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /new claim/i })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Claim status')).toBeDisabled();
  });
});

describe('PatientDetails break-glass', () => {
  it('offers break-glass for a patient outside the user\'s units and sends the reason', async () => {
    const { default: PatientDetails } = await import('./PatientDetails');
    const restricted = Object.assign(new Error('403'), {
      response: { status: 403, data: { detail: 'This patient is outside your units.', break_glass: true } },
    });
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/units') return { data: [] };
      throw restricted;
    });
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { grant_id: 'agr_1' } });
    renderAt('/patients/pat_icu', '/patients/:patientId', <PatientDetails />);

    expect(await screen.findByTestId('break-glass-panel')).toHaveTextContent('outside your units');
    const submit = screen.getByTestId('break-glass-submit');
    await userEvent.type(screen.getByTestId('break-glass-reason'), 'short');
    expect(submit).toBeDisabled(); // under 10 characters
    await userEvent.type(screen.getByTestId('break-glass-reason'), ' - covering ICU tonight');
    await userEvent.click(submit);
    await waitFor(() => expect(post).toHaveBeenCalledWith('/patients/pat_icu/break-glass',
      { reason: 'short - covering ICU tonight' }));
  });
});
