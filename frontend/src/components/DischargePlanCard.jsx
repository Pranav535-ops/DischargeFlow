import React, { useEffect, useState } from 'react';
import { CalendarCheck, Check, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '../context/AuthContext';
import { api, errorMessage } from '@/lib/api';
import { invalidateDischarge, useEncounters, usePlan, useReadOnly } from '@/lib/queries';
import {
  DISPOSITION_LABELS, canManagePlan, formatPlanDate, planReviewBody, planUpdateBody,
} from '@/lib/plans';
import { Button } from './ui/button';
import ReadmissionRiskPanel from './ReadmissionRiskPanel';

// The discharge plan of the current stay (ADR-002 item 8): expected date and disposition, set from admission and
// reviewed daily. The disposition picks the workflow template when the discharge starts (item 9). The readmission
// risk score (item 20) is shown with it, since it is meant to shape the plan.
const DischargePlanCard = ({ patientId }) => {
  const { user } = useAuth();
  const readOnly = useReadOnly();
  const { data: encounters } = useEncounters(patientId);
  const current = encounters?.find(e => e.status === 'active');
  const { data: plan, isLoading } = usePlan(current?.encounter_id);
  const [form, setForm] = useState({ expected_discharge_date: '', planned_disposition: '' });
  const [saving, setSaving] = useState(false);
  const canEdit = canManagePlan(user) && !readOnly;

  useEffect(() => {
    setForm({
      expected_discharge_date: plan?.expected_discharge_date || '',
      planned_disposition: plan?.planned_disposition || '',
    });
  }, [plan?.expected_discharge_date, plan?.planned_disposition]);

  if (!current) return null;

  const send = async (request, done) => {
    setSaving(true);
    try {
      await request();
      toast.success(done);
      await invalidateDischarge();
    } catch (error) {
      toast.error('Could not save the plan', { description: errorMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  const save = () => {
    const body = planUpdateBody(plan, form);
    if (body) send(() => api.put(`/encounters/${current.encounter_id}/plan`, body), 'Discharge plan saved');
  };
  const review = () => {
    const body = planReviewBody(plan);
    if (body) send(() => api.post(`/encounters/${current.encounter_id}/plan/review`, body), 'Plan reviewed');
  };

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6" data-testid="discharge-plan">
      <div className="flex items-center justify-between gap-3 mb-4">
        <div className="flex items-center gap-3">
          <CalendarCheck className="w-5 h-5 text-blue-400" strokeWidth={1.5} />
          <h2 className="text-lg font-medium text-zinc-50">Discharge plan</h2>
        </div>
        {plan && (
          <span className={`text-xs px-2 py-0.5 rounded ${plan.reviewed_today
            ? 'bg-emerald-500/10 text-emerald-400' : 'bg-amber-500/10 text-amber-400'}`}>
            {plan.reviewed_today ? 'Reviewed today' : 'Not reviewed today'}
          </span>
        )}
      </div>
      {isLoading || !plan ? (
        <Loader2 className="w-5 h-5 animate-spin text-blue-500" />
      ) : (
        <div className="space-y-4">
          <dl className="grid grid-cols-1 sm:grid-cols-3 gap-4 text-sm">
            <div>
              <dt className="text-zinc-500">Expected discharge</dt>
              <dd className={plan.expected_date_passed ? 'text-red-400' : 'text-zinc-200'}>
                {formatPlanDate(plan.expected_discharge_date) || 'Not set'}
                {plan.expected_date_passed && ' (passed)'}
              </dd>
            </div>
            <div>
              <dt className="text-zinc-500">Planned disposition</dt>
              <dd className="text-zinc-200">
                {DISPOSITION_LABELS[plan.planned_disposition] || plan.planned_disposition || 'Not set'}
              </dd>
            </div>
            <div>
              <dt className="text-zinc-500">Last review</dt>
              <dd className="text-zinc-200">
                {plan.plan_reviewed_at
                  ? `${new Date(plan.plan_reviewed_at).toLocaleString()} by ${plan.plan_reviewed_by_name || plan.plan_reviewed_by}`
                  : 'Never'}
              </dd>
            </div>
          </dl>

          {canEdit && (
            <div className="flex flex-wrap items-end gap-3">
              <label className="text-xs text-zinc-400 flex flex-col gap-1">
                Expected date
                <input
                  type="date"
                  data-testid="plan-date"
                  value={form.expected_discharge_date}
                  onChange={e => setForm({ ...form, expected_discharge_date: e.target.value })}
                  className="h-9 rounded bg-zinc-950 border border-zinc-700 px-2 text-zinc-200"
                />
              </label>
              <label className="text-xs text-zinc-400 flex flex-col gap-1">
                Disposition
                <select
                  data-testid="plan-disposition"
                  value={form.planned_disposition}
                  onChange={e => setForm({ ...form, planned_disposition: e.target.value })}
                  className="h-9 rounded bg-zinc-950 border border-zinc-700 px-2 text-zinc-200"
                >
                  <option value="">Not set</option>
                  {Object.entries(DISPOSITION_LABELS).map(([code, label]) => (
                    <option key={code} value={code}>{label}</option>
                  ))}
                </select>
              </label>
              <Button size="sm" onClick={save} disabled={saving} data-testid="plan-save">Save plan</Button>
              <Button size="sm" variant="outline" onClick={review} disabled={saving}
                className="border-zinc-700 text-zinc-300" data-testid="plan-review">
                <Check className="w-4 h-4 mr-1" /> Reviewed, no change
              </Button>
            </div>
          )}

          {plan.delays?.length > 0 && (
            <div>
              <h3 className="text-sm text-zinc-400 mb-1">Delays</h3>
              <ul className="text-xs text-zinc-500 space-y-1" data-testid="plan-delays">
                {plan.delays.map(d => (
                  <li key={d.delay_id}>
                    {new Date(d.created_at).toLocaleDateString()}: <span className="text-zinc-300">{d.code}</span>
                    {d.kind === 'expected_date_slip' && d.slip_days != null && ` (${d.slip_days} day${d.slip_days === 1 ? '' : 's'})`}
                    {d.kind === 'task_sla_breach' && ' (task late)'}
                    {d.note && ` - ${d.note}`}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ReadmissionRiskPanel encounterId={current.encounter_id} patientId={patientId} risk={plan.readmission_risk}
            canEdit={canEdit} />
        </div>
      )}
    </div>
  );
};

export default DischargePlanCard;
