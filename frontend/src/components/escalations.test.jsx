import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { api } from '@/lib/api';
import { queryClient } from '@/lib/queries';

// Escalation of overdue tasks (ADR-002 item 12): the admin card.
const LIST = {
  escalations: [{
    escalation_id: 'esc_1', task_id: 'task_1', level: 2, priority: 1, priority_name: 'high', category: 'medication',
    unit_code: '4W', target_role: 'physician', target_user_id: null, recipients: 0, page_status: 'failed',
    escalated_at: new Date().toISOString(), link: '/workflows/wf_1',
  }],
  policy: {
    high: { sla_hours: 4, steps: [{ level: 1, after_breach_hours: 0, page: true }, { level: 2, after_breach_hours: 1, page: true }] },
    low: { sla_hours: 24, steps: [{ level: 1, after_breach_hours: 0, page: false }] },
  },
  supervisor_roles: { nurse: 'physician' },
  pager: 'webhook',
  scheduled: false,
};

const renderCard = async () => {
  const { default: AdminEscalations } = await import('./AdminEscalations');
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><AdminEscalations /></MemoryRouter>
    </QueryClientProvider>,
  );
};

beforeEach(() => {
  vi.restoreAllMocks();
  queryClient.clear();
});

describe('AdminEscalations', () => {
  it('lists active escalations with ids and codes, and runs on demand', async () => {
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/escalations') return { data: LIST };
      if (url === '/downtime/status') return { data: { read_only: false, forced: false, period: null } };
      throw new Error(`unexpected ${url}`);
    });
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { tenants: [
      { tenant_id: 'default', escalated: 1, resolved: 0, paged: 1, page_failures: 0, skipped: null }] } });
    await renderCard();

    const row = await screen.findByTestId('escalation-row');
    expect(row).toHaveTextContent('task_1');
    expect(row).toHaveTextContent('4W');
    expect(row).toHaveTextContent('physician (0)');
    expect(row).toHaveTextContent('page failed');
    expect(screen.getByRole('link', { name: 'task_1' })).toHaveAttribute('href', '/workflows/wf_1');
    const card = screen.getByTestId('escalations-card');
    expect(card).toHaveTextContent('high: 4 h + 1 h, low: 24 h');
    expect(card).toHaveTextContent('No schedule configured');

    await userEvent.click(screen.getByTestId('run-escalations'));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/escalations/run'));
  });

  it('disables the manual run in read-only mode', async () => {
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/escalations') return { data: { ...LIST, escalations: [] } };
      return { data: { read_only: true, forced: false, period: null } };
    });
    await renderCard();
    await waitFor(() => expect(screen.getByTestId('run-escalations')).toBeDisabled());
    expect(screen.getByTestId('escalations-card')).toHaveTextContent('0 active');
  });
});
