import React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft, Loader2, Printer } from 'lucide-react';
import { errorMessage } from '@/lib/api';
import { isRtl } from '@/lib/languages';
import { useInstructionsSheet } from '@/lib/queries';
import { Button } from './ui/button';

// Route: /instructions/:instructionsId/print. The approved patient instructions as a handout (ADR-002 item 18).
// Loading it is an audited PHI read, so it loads once. The app chrome is hidden in print (print:hidden on the
// sidebar and header) and the sheet prints black on white.
const PatientInstructionsPrint = () => {
  const { instructionsId } = useParams();
  const navigate = useNavigate();
  const { data: sheet, isLoading, isError, error } = useInstructionsSheet(instructionsId);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }
  if (isError || !sheet) {
    return (
      <div className="text-center py-16" data-testid="instructions-print-error">
        <p className="text-zinc-400 mb-4">{errorMessage(error) || 'These instructions cannot be printed.'}</p>
        <Button variant="outline" onClick={() => navigate(-1)}>Back</Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4 print:hidden">
        <Button variant="ghost" onClick={() => navigate(`/workflows/${sheet.workflow_id}`)}
                className="text-zinc-400 hover:text-zinc-200">
          <ArrowLeft className="w-4 h-4 mr-2" /> Back to the discharge
        </Button>
        <Button data-testid="print-btn" onClick={() => window.print()} className="bg-blue-600 hover:bg-blue-500">
          <Printer className="w-4 h-4 mr-2" /> Print
        </Button>
      </div>
      {sheet.stale && (
        <p className="text-sm text-amber-400 print:hidden" data-testid="print-stale">
          An addendum was signed after these instructions were drafted. Check them against it before printing.
        </p>
      )}

      <article
        lang={sheet.language}
        dir={isRtl(sheet.language) ? 'rtl' : 'ltr'}
        data-testid="instructions-sheet"
        className="mx-auto max-w-3xl rounded-lg border border-zinc-800 bg-zinc-900 p-8 print:max-w-none print:border-0 print:bg-white print:p-0 print:text-black"
      >
        <header className="mb-6 border-b border-zinc-700 pb-4 print:border-black">
          <h1 className="text-2xl font-semibold text-zinc-50 print:text-black" style={{ fontFamily: 'Outfit, sans-serif' }}>
            {sheet.patient_name}
          </h1>
          <p className="text-xs text-zinc-400 print:text-black" lang="en" dir="ltr">
            Discharge instructions · {sheet.language_name}
            {sheet.approved_at && ` · reviewed ${new Date(sheet.approved_at).toLocaleDateString()}`}
            {sheet.approved_by_name && ` by ${sheet.approved_by_name}`}
          </p>
        </header>
        <pre className="instructions-sheet whitespace-pre-wrap font-sans text-lg leading-relaxed text-zinc-200 print:text-black print:text-base">
          {sheet.content}
        </pre>
      </article>
    </div>
  );
};

export default PatientInstructionsPrint;
