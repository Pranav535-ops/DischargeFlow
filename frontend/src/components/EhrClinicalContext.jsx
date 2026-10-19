import React from 'react';
import { AlertTriangle, ClipboardList, Loader2, Pill, Stethoscope } from 'lucide-react';
import { useClinicalContext } from '@/lib/queries';

// Read-only clinical context from the EHR (ADR-002 item 15): active medications, problems and allergies, read live
// over FHIR with the token of this session's EHR launch. Nothing here is stored by DischargeFlow.

const Section = ({ icon: Icon, title, empty, children, count, testid }) => (
  <div data-testid={testid}>
    <div className="flex items-center gap-1.5 text-xs uppercase tracking-wide text-zinc-500 mb-1.5">
      <Icon className="w-3.5 h-3.5" /> {title} <span className="text-zinc-600">({count})</span>
    </div>
    {count === 0 ? <p className="text-sm text-zinc-500">{empty}</p> : <ul className="space-y-1">{children}</ul>}
  </div>
);

export const MedicationList = ({ medications }) => medications.map((m) => (
  <li key={m.name} className="text-sm text-zinc-200">
    {m.name}{m.dosage && <span className="text-zinc-500"> · {m.dosage}</span>}
  </li>
));

// `sections` picks what to show (the medication-reconciliation task shows medications and allergies only).
// `quiet`: render nothing unless the EHR context is available (no launch → no hint).
const EhrClinicalContext = ({ patientId, sections = ['medications', 'problems', 'allergies'], quiet = false }) => {
  const { data, isLoading, isError } = useClinicalContext(patientId);

  if (isLoading) {
    return quiet ? null : (
      <div className="flex items-center gap-2 text-sm text-zinc-500"><Loader2 className="w-4 h-4 animate-spin" />Reading the EHR…</div>
    );
  }
  if (isError || !data?.available) {
    if (quiet) return null;
    return (
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 text-sm text-zinc-500" data-testid="ehr-context-unavailable">
        <span className="text-zinc-400 font-medium">EHR clinical context: </span>
        {data?.detail || 'not available.'}
      </div>
    );
  }

  const errors = data.errors || {};
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 space-y-4" data-testid="ehr-context">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium text-zinc-200">From the EHR</h2>
        <span className="text-xs text-zinc-500">Read-only · {data.source} · {new Date(data.fetched_at).toLocaleTimeString()}</span>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        {sections.includes('medications') && (
          <Section icon={Pill} title="Active medications" count={data.medications.length} testid="ehr-medications"
                   empty={errors.medications ? `Not available (${errors.medications})` : 'None recorded'}>
            <MedicationList medications={data.medications} />
          </Section>
        )}
        {sections.includes('problems') && (
          <Section icon={Stethoscope} title="Problems" count={data.problems.length} testid="ehr-problems"
                   empty={errors.problems ? `Not available (${errors.problems})` : 'None recorded'}>
            {data.problems.map((p) => <li key={p.name} className="text-sm text-zinc-200">{p.name}</li>)}
          </Section>
        )}
        {sections.includes('allergies') && (
          <Section icon={AlertTriangle} title="Allergies" count={data.allergies.length} testid="ehr-allergies"
                   empty={errors.allergies ? `Not available (${errors.allergies})` : 'No known allergies recorded'}>
            {data.allergies.map((a) => (
              <li key={a.substance} className="text-sm text-rose-300">
                {a.substance}
                {a.reactions?.length > 0 && <span className="text-zinc-500"> · {a.reactions.join(', ')}</span>}
                {a.criticality === 'high' && <span className="ml-1 text-xs text-rose-400">(high)</span>}
              </li>
            ))}
          </Section>
        )}
      </div>
    </div>
  );
};

// For the AI summary panel: say that the EHR lists will be used.
export const EhrSummaryNote = ({ patientId }) => {
  const { data } = useClinicalContext(patientId);
  if (!data?.available) return null;
  return (
    <p className="flex items-center gap-1.5 text-xs text-emerald-400" data-testid="ehr-summary-note">
      <ClipboardList className="w-3.5 h-3.5" />
      The draft will use the EHR's {data.medications.length} active medications, {data.problems.length} problems and {data.allergies.length} allergies.
    </p>
  );
};

export default EhrClinicalContext;
