import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { api } from '@/lib/api';
import { queryClient } from '@/lib/queries';
import PatientInstructionsPanel from './PatientInstructionsPanel';
import PatientInstructionsPrint from './PatientInstructionsPrint';

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

const signedWorkflow = { workflow_id: 'wf_1', patient_id: 'pat_1', discharge_summary: { summary_id: 'sum_1', approved: true } };
const patient = { patient_id: 'pat_1', name: 'Ada Lovelace', preferred_language: 'es' };
const draft = {
  instructions_id: 'pin_1', language: 'es', language_name: 'Spanish', english_readability: false, approved: false,
  stale: false, reading_grade: null, target_grade: 6, generated_by: 'mock',
  content: 'Why you were in the hospital\nYou had pneumonia.\n\nYour medicines\n- amoxicillin: Take 500 mg.',
  generation: {
    model_id: 'mock-1', provider: 'mock', prompt_version: 'patient_instructions@v1',
    grounding_warnings: [{ type: 'omission', value: 'acetaminophen', reason: 'discharge medication in the signed summary but not in the instructions' }],
  },
};

describe('PatientInstructionsPanel', () => {
  it('waits for the signed summary before offering a draft', () => {
    const get = vi.spyOn(api, 'get');
    renderAt('/w', '/w', <PatientInstructionsPanel workflow={{ ...signedWorkflow, discharge_summary: { approved: false } }}
                                                   patient={patient} role="nurse" readOnly={false} />);
    expect(screen.getByTestId('instructions-need-signature')).toBeInTheDocument();
    expect(screen.queryByTestId('generate-instructions-btn')).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
  });

  it('drafts in the preferred language, shows warnings, and approves the edited text', async () => {
    let items = [];
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/ai/patient-instructions?workflow_id=wf_1') return { data: items };
      throw new Error(`unexpected ${url}`);
    });
    const post = vi.spyOn(api, 'post').mockImplementation(async () => { items = [draft]; return { data: draft }; });
    const put = vi.spyOn(api, 'put').mockImplementation(async () => {
      items = [{ ...draft, approved: true, content: 'edited' }];
      return { data: { success: true } };
    });
    renderAt('/w', '/w', <PatientInstructionsPanel workflow={signedWorkflow} patient={patient} role="nurse" readOnly={false} />);

    expect(screen.getByTestId('instructions-language')).toHaveValue('es');
    await userEvent.click(screen.getByTestId('generate-instructions-btn'));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/ai/patient-instructions', { workflow_id: 'wf_1', language: 'es' }));

    const card = await screen.findByTestId('instructions-card');
    expect(within(card).getByText(/Reading grade not measured for Spanish/)).toBeInTheDocument();
    expect(within(card).getByTestId('instructions-warnings')).toHaveTextContent('Discharge medicine missing');
    expect(within(card).getByTestId('instructions-provenance')).toHaveTextContent('it does not translate');
    expect(within(card).queryByTestId('print-instructions-btn')).not.toBeInTheDocument(); // drafts don't print

    await userEvent.click(within(card).getByTestId('edit-instructions-btn'));
    const editor = within(card).getByTestId('instructions-editor');
    await userEvent.clear(editor);
    await userEvent.type(editor, 'edited');
    await userEvent.click(within(card).getByTestId('approve-instructions-btn'));
    await waitFor(() => expect(put).toHaveBeenCalledWith('/ai/patient-instructions/pin_1/approve', { content: 'edited' }));
    expect(await screen.findByTestId('instructions-approved')).toBeInTheDocument();
    expect(screen.getByTestId('print-instructions-btn')).toHaveAttribute('href', '/instructions/pin_1/print');
  });

  it('shows an English draft above the target grade and a stale draft', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ data: [{
      ...draft, language: 'en', language_name: 'English', english_readability: true, reading_grade: 9.2, stale: true,
    }] });
    renderAt('/w', '/w', <PatientInstructionsPanel workflow={signedWorkflow} patient={patient} role="pharmacist" readOnly={false} />);
    expect(await screen.findByTestId('reading-grade')).toHaveTextContent('Reading grade 9.2 (target 6 or below)');
    expect(screen.getByTestId('instructions-stale')).toBeInTheDocument();
    // Pharmacists can read them but not draft or approve.
    expect(screen.queryByTestId('approve-instructions-btn')).not.toBeInTheDocument();
    expect(screen.queryByTestId('generate-instructions-btn')).not.toBeInTheDocument();
  });
});

describe('PatientInstructionsPrint (route /instructions/:id/print)', () => {
  it('renders the approved handout in the patient language and prints it', async () => {
    vi.spyOn(api, 'get').mockImplementation(async (url) => {
      if (url === '/ai/patient-instructions/pin_2/print') {
        return { data: { instructions_id: 'pin_2', workflow_id: 'wf_1', patient_name: 'Ada Lovelace', language: 'ar',
                         language_name: 'Arabic', content: 'تعليمات', approved_at: '2026-10-19T10:00:00Z',
                         approved_by_name: 'Nurse Test', stale: false } };
      }
      throw new Error(`unexpected ${url}`);
    });
    const print = vi.spyOn(window, 'print').mockImplementation(() => {});
    renderAt('/instructions/pin_2/print', '/instructions/:instructionsId/print', <PatientInstructionsPrint />);

    const sheet = await screen.findByTestId('instructions-sheet');
    expect(sheet).toHaveAttribute('lang', 'ar');
    expect(sheet).toHaveAttribute('dir', 'rtl');
    expect(within(sheet).getByText('Ada Lovelace')).toBeInTheDocument();
    expect(within(sheet).getByText(/by Nurse Test/)).toBeInTheDocument();
    await userEvent.click(screen.getByTestId('print-btn'));
    expect(print).toHaveBeenCalled();
  });

  it('refuses a draft with the API message', async () => {
    vi.spyOn(api, 'get').mockRejectedValue({ response: { status: 409, data: { detail: 'Only approved instructions can be printed' } } });
    renderAt('/instructions/pin_3/print', '/instructions/:instructionsId/print', <PatientInstructionsPrint />);
    expect(await screen.findByTestId('instructions-print-error')).toHaveTextContent('Only approved instructions can be printed');
  });
});
