import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { api } from '@/lib/api';
import { queryClient } from '@/lib/queries';
import { LAUNCH_KEY, challengeFor, landingPath, makeVerifier, saveLaunch, takeLaunch } from '@/lib/smart';
import EhrClinicalContext from './EhrClinicalContext';
import SmartLaunch from '../pages/SmartLaunch';

const startSession = vi.fn();
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ startSession }) }));

beforeEach(() => {
  vi.restoreAllMocks();
  startSession.mockReset();
  queryClient.clear();
  sessionStorage.clear();
});

afterEach(() => window.history.replaceState(null, '', '/'));

describe('SMART helpers', () => {
  it('makes an RFC 7636 verifier and its S256 challenge', async () => {
    const verifier = makeVerifier();
    expect(verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    // The same value the server's smart.s256 computes (backend/tests/test_smart.py checks it there too).
    expect(await challengeFor('dBjftJeZ4CVP-mJ92K9mHBaB7qYcRlG3xh1lX8qhQyUe8EOs')).toBe(
      'rPQ5JFnsLOVWO0OE9P42S4wLQjxTT5ZYOYigrxZk4io');
  });

  it('uses the saved launch once, and only for the matching state', () => {
    saveLaunch({ state: 's1', verifier: 'v' });
    expect(takeLaunch('other')).toBeNull();
    expect(sessionStorage.getItem(LAUNCH_KEY)).toBeNull();
    saveLaunch({ state: 's1', verifier: 'v' });
    expect(takeLaunch('s1')).toEqual({ state: 's1', verifier: 'v' });
    expect(takeLaunch('s1')).toBeNull();
  });

  it('lands on the workflow, else the patient, else the list', () => {
    expect(landingPath({ workflow_id: 'wf_1', patient_id: 'pat_1' })).toBe('/workflows/wf_1');
    expect(landingPath({ patient_id: 'pat_1' })).toBe('/patients/pat_1');
    expect(landingPath({ matched: false })).toBe('/patients');
  });
});

const renderLaunch = () => render(
  <MemoryRouter initialEntries={['/launch']}>
    <Routes>
      <Route path="/launch" element={<SmartLaunch />} />
      <Route path="/workflows/:id" element={<p>workflow screen</p>} />
    </Routes>
  </MemoryRouter>,
);

describe('SmartLaunch (/launch)', () => {
  it('exchanges the code with the saved verifier and opens the launch patient', async () => {
    saveLaunch({ state: 'state-1', verifier: 'verifier-1' });
    window.history.replaceState(null, '', '/launch?code=abc&state=state-1');
    const session = { token: 't', user: { role: 'physician' }, launch: { matched: true, patient_id: 'pat_1', workflow_id: 'wf_9' } };
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: session });
    renderLaunch();
    expect(await screen.findByText('workflow screen')).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith('/smart/token', { state: 'state-1', code: 'abc', code_verifier: 'verifier-1' });
    expect(startSession).toHaveBeenCalledWith(session);
    expect(window.location.search).toBe(''); // the code is not left in the address bar
  });

  it('refuses a callback whose state this tab did not start', async () => {
    saveLaunch({ state: 'mine', verifier: 'v' });
    window.history.replaceState(null, '', '/launch?code=abc&state=forged');
    const post = vi.spyOn(api, 'post');
    renderLaunch();
    expect(await screen.findByTestId('smart-launch-error')).toHaveTextContent('did not start in this tab');
    expect(post).not.toHaveBeenCalled();
  });

  it('shows the authorization error from the EHR', async () => {
    window.history.replaceState(null, '', '/launch?error=access_denied&state=x');
    renderLaunch();
    expect(await screen.findByTestId('smart-launch-error')).toHaveTextContent('access_denied');
  });
});

describe('EhrClinicalContext', () => {
  const renderCtx = (props) => render(
    <QueryClientProvider client={queryClient}><EhrClinicalContext patientId="pat_1" {...props} /></QueryClientProvider>,
  );

  it('shows the EHR lists', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ data: {
      available: true, source: 'EHR (SMART on FHIR)', fetched_at: '2026-10-19T10:00:00Z', errors: {},
      medications: [{ name: 'Lisinopril 10 MG', dosage: '10 mg daily', status: 'active' }],
      problems: [{ name: 'Type 2 diabetes mellitus' }],
      allergies: [{ substance: 'Penicillin', criticality: 'high', reactions: ['Hives'] }],
    } });
    renderCtx();
    expect(await screen.findByTestId('ehr-medications')).toHaveTextContent('Lisinopril 10 MG · 10 mg daily');
    expect(screen.getByTestId('ehr-problems')).toHaveTextContent('Type 2 diabetes mellitus');
    expect(screen.getByTestId('ehr-allergies')).toHaveTextContent('Penicillin · Hives');
  });

  it('explains when there is no EHR launch, or says nothing when quiet', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ data: { available: false, reason: 'no_launch', detail: 'Open this patient from the EHR' } });
    const { unmount } = renderCtx();
    expect(await screen.findByTestId('ehr-context-unavailable')).toHaveTextContent('Open this patient from the EHR');
    unmount();
    queryClient.clear();
    const { container } = renderCtx({ quiet: true });
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
    expect(container).toBeEmptyDOMElement();
  });
});
