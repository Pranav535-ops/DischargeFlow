import React, { useEffect, useState } from 'react';
import { Activity, Plus } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { invalidateDischarge, useClinicalContext, usePlanCodes } from '@/lib/queries';
import { riskInputsBody } from '@/lib/plans';
import { Button } from './ui/button';

const formFrom = (inputs) => ({
  admission_type: inputs?.admission_type || '',
  comorbidities: inputs?.comorbidities || [],
  ed_visits_recorded: inputs?.ed_visits_recorded ?? '',
});

// Readmission risk on the discharge plan (ADR-002 item 20): the LACE index, a published score over structured data
// (not an AI prediction). Every point is shown with the input it came from. When the score is high, extra tasks are
// suggested; the clinician picks which to add. With an EHR launch, Charlson comorbidities coded on the EHR problem
// list are proposed (Quan et al. 2005 ICD-10 algorithm), unchecked: they count only once a clinician ticks and saves
// them through the normal risk-inputs save.
const ReadmissionRiskPanel = ({ encounterId, patientId, risk, canEdit }) => {
  const { data: codes } = usePlanCodes();
  const { data: ehr } = useClinicalContext(patientId);
  const [form, setForm] = useState(formFrom(risk?.inputs));
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);

  // Reset the form when the saved inputs change (not on every refetch of the same values).
  const inputsKey = JSON.stringify(risk?.inputs ?? null);
  useEffect(() => { setForm(formFrom(JSON.parse(inputsKey))); }, [inputsKey]);

  if (!risk) return null;

  const proposals = ehr?.available ? ehr.charlson_proposals || [] : [];
  const proposed = new Set(proposals.map(p => p.key));
  const recorded = new Set(risk.inputs?.comorbidities || []);

  const send = async (request, done) => {
    setSaving(true);
    try {
      await request();
      toast.success(done);
      await invalidateDischarge();
      setEditing(false);
    } catch (error) {
      toast.error('Could not save', { description: errorMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  const save = () => {
    const body = riskInputsBody(risk.inputs, form);
    if (!body) { setEditing(false); return; }
    send(() => api.put(`/encounters/${encounterId}/risk-inputs`, body), 'Risk inputs saved');
  };
  const addTask = (key) => send(() => api.post(`/encounters/${encounterId}/risk-tasks`, { keys: [key] }), 'Task added');
  const toggle = (key) => setForm(f => ({
    ...f, comorbidities: f.comorbidities.includes(key) ? f.comorbidities.filter(k => k !== key) : [...f.comorbidities, key],
  }));
  const accept = (key) => { setEditing(true); toggle(key); };

  return (
    <div className="border-t border-zinc-800 pt-4" data-testid="readmission-risk">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
        <div className="flex items-center gap-2">
          <Activity className="w-4 h-4 text-blue-400" strokeWidth={1.5} />
          <h3 className="text-sm font-medium text-zinc-200">Readmission risk ({risk.method})</h3>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-zinc-200 text-sm" data-testid="risk-score">{risk.score} / {risk.max_score}</span>
          <span className={`text-xs px-2 py-0.5 rounded ${risk.high_risk
            ? 'bg-red-500/10 text-red-400' : 'bg-emerald-500/10 text-emerald-400'}`} data-testid="risk-level">
            {risk.high_risk ? `High risk (${risk.threshold} or more)` : `Below ${risk.threshold}`}
          </span>
        </div>
      </div>
      {!risk.complete && (
        <p className="text-xs text-amber-400 mb-2" data-testid="risk-partial">
          Partial score: inputs not recorded count 0, so the real score may be higher.
        </p>
      )}
      <ul className="space-y-1 text-sm" data-testid="risk-components">
        {risk.components.map(c => (
          <li key={c.key} className="flex gap-3">
            <span className="w-6 text-zinc-500 font-mono">{c.letter}</span>
            <span className="w-14 text-zinc-200">{c.points} / {c.max_points}</span>
            <span className="text-zinc-400"><span className="text-zinc-300">{c.label}:</span> {c.explanation}</span>
          </li>
        ))}
      </ul>

      {risk.suggested_tasks?.length > 0 && (
        <div className="mt-3" data-testid="risk-suggestions">
          <h4 className="text-xs text-zinc-400 mb-1">Suggested for a high risk (add the ones that fit this patient)</h4>
          <ul className="space-y-1">
            {risk.suggested_tasks.map(t => (
              <li key={t.key} className="flex items-center gap-2 text-sm">
                {t.added ? (
                  <span className="text-xs text-emerald-400 w-16">Added</span>
                ) : canEdit ? (
                  <Button size="sm" variant="outline" className="h-7 border-zinc-700 text-zinc-300 w-16"
                    disabled={saving} onClick={() => addTask(t.key)} data-testid={`risk-add-${t.key}`}>
                    <Plus className="w-3 h-3 mr-1" /> Add
                  </Button>
                ) : <span className="w-16" />}
                <span className="text-zinc-300" title={t.description}>{t.title}</span>
                <span className="text-xs text-zinc-500">({t.assigned_role})</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {proposals.length > 0 && (
        <div className="mt-3" data-testid="risk-proposals" title={ehr.charlson_citation}>
          <h4 className="text-xs text-zinc-400 mb-1">
            Proposed from the EHR problem list (not counted until a clinician confirms and saves)
          </h4>
          <ul className="space-y-1">
            {proposals.map(p => (
              <li key={p.key} className="flex items-start gap-2 text-sm" data-testid={`risk-proposal-${p.key}`}>
                {recorded.has(p.key) ? (
                  <span className="text-xs text-emerald-400 w-16 shrink-0">Recorded</span>
                ) : canEdit ? (
                  <input type="checkbox" className="mt-1" checked={form.comorbidities.includes(p.key)}
                    onChange={() => accept(p.key)} aria-label={`Confirm ${p.label}`}
                    data-testid={`risk-proposal-accept-${p.key}`} />
                ) : <span className="w-16 shrink-0" />}
                <span className="text-zinc-300">{p.label} ({p.weight})</span>
                <span className="text-xs text-zinc-500">
                  from {p.sources.map(s => `${s.condition} (${s.system} ${s.code})`).join('; ')}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {canEdit && !editing && (
        <Button size="sm" variant="outline" className="mt-3 border-zinc-700 text-zinc-300"
          onClick={() => setEditing(true)} data-testid="risk-edit">Record risk inputs</Button>
      )}
      {canEdit && editing && codes && (
        <div className="mt-3 space-y-3" data-testid="risk-form">
          <div className="flex flex-wrap gap-3">
            <label className="text-xs text-zinc-400 flex flex-col gap-1">
              Admission type
              <select data-testid="risk-admission-type" value={form.admission_type}
                onChange={e => setForm({ ...form, admission_type: e.target.value })}
                className="h-9 rounded bg-zinc-950 border border-zinc-700 px-2 text-zinc-200">
                <option value="">Not recorded</option>
                {codes.admission_types.map(t => (
                  <option key={t.code} value={t.code}>{t.label}{t.acute ? ' (acute)' : ''}</option>
                ))}
              </select>
            </label>
            <label className="text-xs text-zinc-400 flex flex-col gap-1">
              ED visits, last 6 months
              <input type="number" min="0" max="99" data-testid="risk-ed-visits" value={form.ed_visits_recorded}
                placeholder={`counted: ${risk.inputs.ed_visits_counted ?? 0}`}
                onChange={e => setForm({ ...form, ed_visits_recorded: e.target.value })}
                className="h-9 w-40 rounded bg-zinc-950 border border-zinc-700 px-2 text-zinc-200" />
            </label>
          </div>
          <fieldset>
            <legend className="text-xs text-zinc-400 mb-1">Charlson comorbidities (weight)</legend>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-1">
              {codes.comorbidities.map(c => (
                <label key={c.key} className="flex items-center gap-2 text-sm text-zinc-300">
                  <input type="checkbox" checked={form.comorbidities.includes(c.key)} onChange={() => toggle(c.key)}
                    data-testid={`risk-comorbidity-${c.key}`} />
                  {c.label} ({c.weight})
                  {proposed.has(c.key) && <span className="text-xs text-blue-400">proposed by the EHR</span>}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="flex gap-2">
            <Button size="sm" onClick={save} disabled={saving} data-testid="risk-save">Save risk inputs</Button>
            <Button size="sm" variant="outline" className="border-zinc-700 text-zinc-300"
              onClick={() => { setForm(formFrom(risk.inputs)); setEditing(false); }}>Cancel</Button>
          </div>
        </div>
      )}
      <p className="mt-3 text-xs text-zinc-600" title={risk.citation}>
        A published score over structured data (van Walraven et al., CMAJ 2010), not an AI prediction.
        Version {risk.version}.
      </p>
    </div>
  );
};

export default ReadmissionRiskPanel;
