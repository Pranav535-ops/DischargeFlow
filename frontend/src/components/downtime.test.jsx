import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { api } from '@/lib/api';

// Downtime procedure (ADR-002 item 6): banner, disabled writes, back-entry of paper work, census.
vi.mock('@/context/AuthContext', () => ({ useAuth: () => ({ user: { user_id: 'u_n', role: 'nurse', name: 'Nurse' } }) }));
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { user_id: 'u_n', role: 'nurse', name: 'Nurse' } }) }));

const renderAt = (path, routePath, element) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={routePath} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

const READ_ONLY = {
  read_only: true, forced: false,
  period: { downtime_id: 'dt_1', kind: 'read_only', reason: 'Database maintenance until 02:00', started_at: '2026-10-19T00:00:00Z' },
};
const NORMAL = { read_only: false, forced: false, period: null };
const OUTAGE = { downtime_id: 'dt_0', kind: 'outage', reason: 'Network', started_at: '2026-10-19T08:00:00Z', ended_at: '2026-10-19T10:00:00Z' };

const workflow = {
  workflow_id: 'wf_1', patient_id: 'pat_1', status: 'in_progress', started_at: '2026-10-19T07:00:00Z',
  discharge_summary: null,
  tasks: [{ task_id: 'task_1', title: 'Final Vitals Check', description: '', category: 'documentation',
    assigned_role: 'nurse', assigned_to: null, status: 'pending', priority: 1 }],
};

const mockGets = (status, periods = []) => vi.spyOn(api, 'get').mockImplementation(async (url) => {
  if (url === '/downtime/status') return { data: status };
  if (url === '/downtime/periods') return { data: periods };
  if (url === '/workflows/wf_1') return { data: workflow };
  if (url === '/patients/pat_1') return { data: { patient_id: 'pat_1', name: 'Jane Doe', room_number: '12A' } };
  throw new Error(`unexpected ${url}`);
});

beforeEach(() => vi.restoreAllMocks());

describe('DowntimeBanner', () => {
  it('shows the reason while read-only, and nothing otherwise', async () => {
    const { default: DowntimeBanner } = await import('./DowntimeBanner');
    mockGets(READ_ONLY);
    renderAt('/', '/', <DowntimeBanner />);
    const banner = await screen.findByTestId('downtime-banner');
    expect(banner).toHaveTextContent('read-only');
    expect(banner).toHaveTextContent('Database maintenance until 02:00');
    expect(screen.getByRole('link', { name: /print census/i })).toHaveAttribute('href', '/census');
  });

  it('renders nothing when writable', async () => {
    const { default: DowntimeBanner } = await import('./DowntimeBanner');
    const get = mockGets(NORMAL);
    const { container } = renderAt('/', '/', <DowntimeBanner />);
    await waitFor(() => expect(get).toHaveBeenCalledWith('/downtime/status'));
    expect(container).toBeEmptyDOMElement();
  });
});

describe('WorkflowView during and after a downtime', () => {
  it('disables task controls while read-only', async () => {
    const { default: WorkflowView } = await import('./WorkflowView');
    mockGets(READ_ONLY, [OUTAGE]);
    renderAt('/workflows/wf_1', '/workflows/:workflowId', <WorkflowView />);
    const box = await screen.findByTestId('task-checkbox-task_1');
    await waitFor(() => expect(box).toBeDisabled());
    expect(screen.queryByTestId('paper-time-task_1')).not.toBeInTheDocument();
  });

  it('back-enters a task done on paper with the time it was done', async () => {
    const { default: WorkflowView } = await import('./WorkflowView');
    mockGets(NORMAL, [OUTAGE]);
    const put = vi.spyOn(api, 'put').mockResolvedValue({ data: {} });
    renderAt('/workflows/wf_1', '/workflows/:workflowId', <WorkflowView />);

    const input = await screen.findByTestId('paper-time-task_1');
    await userEvent.type(input, '2026-10-19T09:15');
    await userEvent.click(screen.getByTestId('paper-enter-task_1'));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/tasks/task_1/backfill', {
      completed_at: new Date('2026-10-19T09:15').toISOString(),
    }));
  });

  it('offers no back-entry when no downtime was recorded', async () => {
    const { default: WorkflowView } = await import('./WorkflowView');
    mockGets(NORMAL, []);
    renderAt('/workflows/wf_1', '/workflows/:workflowId', <WorkflowView />);
    expect(await screen.findByTestId('task-checkbox-task_1')).not.toBeDisabled();
    expect(screen.queryByTestId('paper-time-task_1')).not.toBeInTheDocument();
  });
});

describe('CensusView', () => {
  it('shows each patient with discharge status and open tasks, and prints', async () => {
    const { default: CensusView } = await import('./CensusView');
    vi.spyOn(api, 'get').mockImplementation(async (url, config) => {
      if (url === '/units') return { data: [{ unit_id: 'unit_4w', code: '4W', name: '4 West' }] };
      if (url === '/census') {
        return { data: {
          generated_at: '2026-10-19T06:00:00Z', unit: config?.params?.unit_id ? { unit_id: 'unit_4w', code: '4W', name: '4 West' } : null,
          totals: { patients: 1, in_discharge: 1, pending_approval: 0, open_tasks: 1 },
          patients: [{ patient_id: 'pat_1', unit_code: '4W', room_number: '12A', name: 'Jane Doe', mrn: 'M1',
            date_of_birth: '1970-01-01', admission_date: '2026-10-15', attending_physician: 'Dr Test', status: 'pending_discharge',
            workflow_id: 'wf_1', discharge_status: 'pending_approval', estimated_discharge_time: null, summary_approved: true,
            open_tasks: [{ task_id: 'task_1', title: 'Final Medication Review', assigned_role: 'pharmacist',
              assigned_to_name: null, priority: 1, status: 'pending' }] }],
        } };
      }
      throw new Error(`unexpected ${url}`);
    });
    const print = vi.spyOn(window, 'print').mockImplementation(() => {});
    renderAt('/census', '/census', <CensusView />);

    const table = await screen.findByTestId('census-table');
    expect(table).toHaveTextContent('Jane Doe');
    expect(table).toHaveTextContent('Pending approval');
    expect(table).toHaveTextContent('Final Medication Review (pharmacist, High)');
    await userEvent.selectOptions(screen.getByTestId('census-unit'), 'unit_4w');
    expect(await screen.findByText(/Discharge census: 4W · 4 West/)).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/census', { params: { unit_id: 'unit_4w' } });
    await userEvent.click(screen.getByTestId('census-print'));
    expect(print).toHaveBeenCalledOnce();
  });
});
