import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { api } from '@/lib/api';
import { queryClient } from '@/lib/queries';

const auth = vi.hoisted(() => ({ user: { user_id: 'user_n', role: 'nurse', name: 'Charge Nurse' } }));
vi.mock('@/context/AuthContext', () => ({ useAuth: () => auth }));
vi.mock('../context/AuthContext', () => ({ useAuth: () => auth }));

const renderAt = (path, routePath, element) => render(
  <QueryClientProvider client={queryClient}>
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={routePath} element={element} />
      </Routes>
    </MemoryRouter>
  </QueryClientProvider>,
);

beforeEach(() => {
  vi.restoreAllMocks();
  queryClient.clear();
  auth.user = { user_id: 'user_n', role: 'nurse', name: 'Charge Nurse' };
});

const column = (over) => ({
  unit_id: 'unit_4w', unit_code: '4W', unit_name: '4 West', in_house: 3, expected: 2, expected_by_hour: { 10: 1 },
  unscheduled: 1, expected_tomorrow: 1, past_expected_date: 0, no_expected_date: 0, discharged: 1,
  discharged_by_hour: { 9: 1 }, discharged_before_noon: 1, beds_dirty: 1, beds_cleaning: 0, beds_available: 0,
  before_noon_7d: { n: 4, before_noon: 3, rate: 0.75 }, turnaround_minutes_7d: { n: 0, median: null }, ...over,
});
const board = {
  date: '2026-10-19', today: '2026-10-19', timezone: 'UTC', noon_hour: 12, unit_scoped: true,
  units: [column({})],
  totals: { in_house: 3, expected: 2, unscheduled: 1, expected_tomorrow: 1, past_expected_date: 0, no_expected_date: 0,
    discharged: 1, discharged_before_noon: 1, beds_dirty: 1, beds_cleaning: 0, beds_available: 0,
    expected_by_hour: { 10: 1 }, before_noon_7d: { n: 4, before_noon: 3, rate: 0.75 } },
  beds: [{ turnover_id: 'bed_1', unit_id: 'unit_4w', room_number: '401', status: 'dirty', source: 'discharge',
    dirty_at: '2026-10-19T09:00:00Z', cleaning_started_at: null, available_at: null, minutes_out_of_service: 75 }],
};

const mockGet = (data) => vi.spyOn(api, 'get').mockImplementation(async (url) => {
  if (url === '/capacity/board') return { data };
  if (url === '/downtime/status') return { data: { read_only: false } };
  throw new Error(`unexpected ${url}`);
});

describe('CapacityBoardView (route /capacity)', () => {
  it('shows expected discharges by unit and hour, the before-noon rate, and advances a dirty bed', async () => {
    const { default: CapacityBoardView } = await import('./CapacityBoardView');
    mockGet(board);
    const put = vi.spyOn(api, 'put').mockResolvedValue({ data: {} });
    renderAt('/capacity', '/capacity', <CapacityBoardView />);

    const row = await screen.findByTestId('hour-row-4W');
    expect(within(row).getByText('4W - 4 West')).toBeInTheDocument();
    const cells = within(row).getAllByRole('cell');
    expect(cells[1 + 3]).toHaveTextContent('1'); // the 10:00 column (the strip starts at 07:00)
    expect(within(screen.getByTestId('tile-before-noon')).getByText('75%')).toBeInTheDocument();
    expect(within(screen.getByTestId('tile-beds')).getByText('1 dirty · 0 cleaning')).toBeInTheDocument();

    const bed = screen.getByTestId('bed-bed_1');
    expect(within(bed).getByText('Room 401')).toBeInTheDocument();
    expect(within(bed).getByText(/1 h 15 min out of service/)).toBeInTheDocument();
    await userEvent.click(within(bed).getByRole('button', { name: 'Start cleaning' }));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/beds/bed_1', { status: 'cleaning' }));
  });

  it('shows no bed actions to roles without bed:manage', async () => {
    auth.user = { user_id: 'user_p', role: 'physician', name: 'Dr Board' };
    const { default: CapacityBoardView } = await import('./CapacityBoardView');
    mockGet({ ...board, beds: [{ ...board.beds[0], status: 'cleaning', cleaning_started_at: '2026-10-19T10:00:00Z' }] });
    renderAt('/capacity', '/capacity', <CapacityBoardView />);
    const bed = await screen.findByTestId('bed-bed_1');
    expect(within(bed).getByText('Cleaning')).toBeInTheDocument();
    expect(within(bed).queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('OutcomesPanel (on /insights)', () => {
  it('shows outcomes by unit with suppressed small groups and the delay Pareto', async () => {
    const { default: OutcomesPanel } = await import('./OutcomesPanel');
    const group = (n, rate) => ({
      discharges: n,
      length_of_stay_days: rate ? { n, avg_days: 4.2, median_days: 4 } : { n },
      vs_expected_date: rate ? { n, avg_days: 0.5, median_days: 0, on_or_before: { n, count: 2 } } : { n },
      expected_date_accuracy: { final_exact: rate ? { n, count: 2, rate: 0.5 } : { n, count: 0 },
        first_exact: { n: 2, count: 1 } },
      before_noon: rate ? { n, count: 1, rate: 0.25 } : { n, count: 0 },
      readmitted_within_30_days: { n, count: 0, follow_up_pending: 0 },
    });
    const outcomes = {
      window_days: 90, min_group_size: 3, overall: group(4, true), by_unit: { ICU: group(1, false) },
      delay_reasons: { recorded: 4, pareto: [
        { code: 'social', n: 3, share: 0.75, cumulative_share: 0.75 },
        { code: 'placement', n: 1, share: 0.25, cumulative_share: 1 }] },
      notes: { attribution: 'by the unit of the stay', first_expected_date: 'from 0018', readmission: 'within 30 days' },
    };
    const get = vi.spyOn(api, 'get').mockResolvedValue({ data: outcomes });
    renderAt('/insights', '/insights', <OutcomesPanel />);

    const all = await screen.findByTestId('outcomes-all');
    expect(within(all).getByText('25%')).toBeInTheDocument();
    expect(within(all).getByText('+0.5 d')).toBeInTheDocument();
    const icu = screen.getByTestId('outcomes-unit-ICU');
    expect(within(icu).getAllByText(/too few/).length).toBeGreaterThan(0);
    const pareto = screen.getByTestId('delay-pareto');
    expect(within(pareto).getByText('social')).toBeInTheDocument();
    expect(within(pareto).getByText('3 · 75% (cum. 75%)')).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Outcome window'), '30');
    await waitFor(() => expect(get).toHaveBeenCalledWith('/analytics/outcomes?days=30'));
  });
});
