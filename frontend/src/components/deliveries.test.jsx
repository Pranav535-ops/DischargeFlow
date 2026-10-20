import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { api } from '@/lib/api';
import { queryClient } from '@/lib/queries';

// Outbound summary delivery (ADR-002 item 16): the worklist and the recipients card.
const FAILED = {
  delivery_id: 'dlv_1', workflow_id: 'wf_1', link: '/workflows/wf_1', endpoint_id: 'dep_1',
  endpoint_name: 'Riverside Family Practice', document_format: 'fhir_bundle', status: 'failed', attempts: 7,
  last_error: 'http_503', last_attempt_at: new Date().toISOString(), trigger_id: 'sver_1',
};
const NOT_READ_ONLY = { data: { read_only: false, forced: false, period: null } };

const wrap = (ui) => render(
  <QueryClientProvider client={queryClient}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>,
);

beforeEach(() => {
  vi.restoreAllMocks();
  queryClient.clear();
});

describe('DeliveriesView', () => {
  it('lists failed deliveries and retries one', async () => {
    let rows = [FAILED];
    const get = vi.spyOn(api, 'get').mockImplementation(async (url, config) => {
      if (url === '/deliveries') return { data: config.params.status === 'failed' ? rows : [] };
      if (url === '/downtime/status') return NOT_READ_ONLY;
      throw new Error(`unexpected ${url}`);
    });
    const post = vi.spyOn(api, 'post').mockImplementation(async () => {
      rows = [];
      return { data: { ...FAILED, status: 'queued' } };
    });
    const { default: DeliveriesView } = await import('./SummaryDelivery');
    wrap(<DeliveriesView />);

    const row = await screen.findByTestId('delivery-row');
    expect(row).toHaveTextContent('Riverside Family Practice');
    expect(row).toHaveTextContent('FHIR document');
    expect(row).toHaveTextContent('http_503');
    expect(screen.getByRole('link', { name: 'wf_1' })).toHaveAttribute('href', '/workflows/wf_1');
    expect(get).toHaveBeenCalledWith('/deliveries', { params: { status: 'failed' } });

    await userEvent.click(screen.getByRole('button', { name: /retry/i }));
    expect(post).toHaveBeenCalledWith('/deliveries/dlv_1/retry');
    await waitFor(() => expect(screen.getByTestId('deliveries-empty')).toBeInTheDocument());
  });
});

describe('SummaryRecipients', () => {
  it('shows recipients with their delivery state and adds one', async () => {
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/encounters/enc_1/recipients') return { data: [{
        recipient_id: 'drc_1', endpoint_id: 'dep_1', endpoint_name: 'Riverside Family Practice',
        endpoint_kind: 'primary_care', document_format: 'fhir_bundle', endpoint_active: true }] };
      if (url === '/delivery-endpoints') return { data: [
        { endpoint_id: 'dep_1', name: 'Riverside Family Practice' },
        { endpoint_id: 'dep_2', name: 'St. Mary Skilled Nursing' }] };
      if (url === '/deliveries') return { data: [{ ...FAILED, status: 'acknowledged', last_error: null }] };
      if (url === '/downtime/status') return NOT_READ_ONLY;
      throw new Error(`unexpected ${url}`);
    });
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: { queued: ['dlv_2'] } });
    const { SummaryRecipients } = await import('./SummaryDelivery');
    wrap(<SummaryRecipients workflow={{ workflow_id: 'wf_1', encounter_id: 'enc_1' }} canManage />);

    const row = await screen.findByTestId('recipient-row');
    expect(row).toHaveTextContent('Riverside Family Practice');
    await waitFor(() => expect(row).toHaveTextContent('Signed summary: delivered'));
    // Only endpoints not already recipients are offered.
    const select = screen.getByLabelText('Recipient');
    await waitFor(() => expect(select.querySelectorAll('option')).toHaveLength(2));
    await userEvent.selectOptions(select, 'dep_2');
    await userEvent.click(screen.getByTestId('add-recipient'));
    expect(post).toHaveBeenCalledWith('/encounters/enc_1/recipients', { endpoint_id: 'dep_2' });
  });
});
