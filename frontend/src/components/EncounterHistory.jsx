import React from 'react';
import { useNavigate } from 'react-router-dom';
import { BedDouble, Loader2 } from 'lucide-react';
import { useEncounters, useUnits } from '@/lib/queries';

const fmtDate = (value) => (value ? new Date(value).toLocaleDateString() : null);

const WORKFLOW_LABELS = {
  in_progress: 'In progress', pending_approval: 'Pending approval', completed: 'Completed',
  externally_discharged: 'Patient left, tasks open',
};
const ENCOUNTER_LABELS = { active: 'Current stay', discharged: 'Discharged', cancelled: 'Admission cancelled' };

// Every stay of one patient, newest first (ADR-002 item 7): a readmission is a new encounter, so earlier stays keep
// their dates, location and discharge workflow.
const EncounterHistory = ({ patientId }) => {
  const navigate = useNavigate();
  const { data: encounters, isLoading } = useEncounters(patientId);
  const { data: units = [] } = useUnits();
  const unitCode = (id) => units.find(u => u.unit_id === id)?.code;

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6" data-testid="encounter-history">
      <div className="flex items-center gap-3 mb-4">
        <BedDouble className="w-5 h-5 text-blue-400" strokeWidth={1.5} />
        <h2 className="text-lg font-medium text-zinc-50">Encounters</h2>
      </div>
      {isLoading ? (
        <Loader2 className="w-5 h-5 animate-spin text-blue-500" />
      ) : !encounters?.length ? (
        <p className="text-sm text-zinc-500">No encounters recorded.</p>
      ) : (
        <ul className="divide-y divide-zinc-800">
          {encounters.map((e) => (
            <li key={e.encounter_id} className="py-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
              <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                e.status === 'active' ? 'bg-blue-500/10 text-blue-400 border border-blue-500/20' : 'status-discharged'
              }`}>
                {ENCOUNTER_LABELS[e.status] || e.status}
              </span>
              <span className="text-zinc-200">
                {fmtDate(e.admitted_at) || 'Admission date unknown'}
                {' – '}
                {e.discharged_at ? fmtDate(e.discharged_at) : e.status === 'active' ? 'now' : 'unknown'}
              </span>
              {(e.unit_id || e.room_number) && (
                <span className="text-zinc-400">
                  {[unitCode(e.unit_id) && `Unit ${unitCode(e.unit_id)}`, e.room_number && `Room ${e.room_number}`]
                    .filter(Boolean).join(' · ')}
                </span>
              )}
              {e.visit_number && <span className="text-zinc-500">Visit {e.visit_number}</span>}
              {e.disposition && <span className="text-zinc-500">Disposition {e.disposition}</span>}
              {e.workflows.map((w) => (
                <button
                  key={w.workflow_id}
                  type="button"
                  onClick={() => navigate(`/workflows/${w.workflow_id}`)}
                  className="text-blue-400 hover:text-blue-300 underline-offset-2 hover:underline"
                >
                  Discharge workflow: {WORKFLOW_LABELS[w.status] || w.status}
                </button>
              ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};

export default EncounterHistory;
