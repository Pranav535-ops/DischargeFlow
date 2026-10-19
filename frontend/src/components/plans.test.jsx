import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { api } from '@/lib/api';
import { queryClient } from '@/lib/queries';

vi.mock('@/context/AuthContext', () => ({ useAuth: () => ({ user: { user_id: 'user_n', role: 'nurse', name: 'Nurse Test' } }) }));
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: { user_id: 'user_n', role: 'nurse', name: 'Nurse Test' } }) }));

const renderAt = (path, routePath, element) => render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={routePath} element={element} />
        <Route path="*" element={<p>elsewhere</p>} />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>,
);

beforeEach(() => {
  vi.restoreAllMocks();
  queryClient.clear();
});

const reviewList = {
  today: '2026-10-19',
  units: [{
    unit_id: 'unit_4w', unit_code: '4W', unit_name: '4 West', total: 2, not_reviewed_today: 1,
    plans: [
      { encounter_id: 'enc_1', patient_id: 'pat_1', patient_name: 'Jane Doe', room_number: '12A',
        expected_discharge_date: '2026-10-17', planned_disposition: 'snf', reviewed_today: false,
        expected_date_passed: true, plan_reviewed_at: null },
      { encounter_id: 'enc_2', patient_id: 'pat_2', patient_name: 'John Roe', room_number: '14',
        expected_discharge_date: '2026-10-21', planned_disposition: 'home', reviewed_today: true,
        expected_date_passed: false, plan_reviewed_at: '2026-10-19T08:00:00Z', plan_reviewed_by_name: 'Dr Rounds' },
    ],
  }],
};

describe('PlanReviewView (route /plans)', () => {
  it('lists the plans not reviewed today per unit and asks why a passed date slipped', async () => {
    const { default: PlanReviewView } = await import('./PlanReviewView');
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/discharge-plans/review') return { data: reviewList };
      if (url === '/units') return { data: [{ unit_id: 'unit_4w', code: '4W', name: '4 West' }] };
      if (url === '/downtime/status') return { data: { read_only: false } };
      throw new Error(`unexpected ${url}`);
    });
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: {} });
    vi.spyOn(window, 'prompt').mockReturnValue('placement no SNF bed yet');
    renderAt('/plans', '/plans', <PlanReviewView />);

    const unit = await screen.findByTestId('plan-unit-4W');
    expect(within(unit).getByText('1 of 2 not reviewed today')).toBeInTheDocument();
    expect(within(unit).getByText('Jane Doe')).toBeInTheDocument();
    expect(within(unit).queryByText('John Roe')).not.toBeInTheDocument(); // reviewed today: hidden by default

    await userEvent.click(screen.getByTestId('plan-review-pat_1'));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/encounters/enc_1/plan/review',
      { delay_code: 'placement', delay_note: 'no SNF bed yet' }));

    await userEvent.click(screen.getByTestId('pending-only'));
    expect(await screen.findByText('John Roe')).toBeInTheDocument();
  });
});

describe('WorkflowView task prerequisites and due times', () => {
  it('shows what a task is waiting on, disables it, and asks for a delay reason on a late task', async () => {
    const { default: WorkflowView } = await import('./WorkflowView');
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/workflows/wf_1') {
        return { data: {
          workflow_id: 'wf_1', patient_id: 'pat_1', status: 'in_progress', started_at: '2026-10-18T10:00:00Z',
          template_id: 'wft_1', template_version: 2,
          plan: { expected_discharge_date: '2026-10-21', planned_disposition: 'snf', expected_date_passed: false },
          tasks: [
            { task_id: 'task_a', title: 'Secure SNF Placement', assigned_role: 'nurse', status: 'pending', priority: 1,
              due: '2026-10-19T00:00:00Z', overdue: true, delay_recorded: false, blocked_by: [] },
            { task_id: 'task_b', title: 'Insurance Prior Authorization', assigned_role: 'nurse', status: 'pending',
              priority: 1, due: '2026-10-20T00:00:00Z', overdue: false, blocked_by: [{ task_id: 'task_a', title: 'Secure SNF Placement' }] },
          ],
          discharge_summary: null,
        } };
      }
      if (url === '/patients/pat_1') return { data: { patient_id: 'pat_1', name: 'Jane Doe', room_number: '12A' } };
      if (url === '/downtime/status') return { data: { read_only: false } };
      if (url === '/downtime/periods') return { data: [] };
      throw new Error(`unexpected ${url}`);
    });
    const put = vi.spyOn(api, 'put').mockResolvedValue({ data: {} });
    vi.spyOn(window, 'prompt').mockReturnValue('placement');
    renderAt('/workflows/wf_1', '/workflows/:workflowId', <WorkflowView />);

    expect(await screen.findByTestId('task-blocked-task_b')).toHaveTextContent('Waiting on: Secure SNF Placement');
    expect(screen.getByTestId('task-checkbox-task_b')).toBeDisabled();
    expect(screen.getByTestId('task-due-task_a')).toHaveTextContent('Overdue since');
    expect(screen.getByTestId('workflow-plan')).toHaveTextContent('Skilled nursing facility');
    expect(screen.getByText('Template v2')).toBeInTheDocument();

    await userEvent.click(screen.getByTestId('task-checkbox-task_a'));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/tasks/task_a', { status: 'completed', delay_code: 'placement' }));
  });
});
