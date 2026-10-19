import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CalendarCheck, Check, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '../context/AuthContext';
import { api, errorMessage } from '@/lib/api';
import { invalidateDischarge, usePlanReview, useReadOnly, useUnits } from '@/lib/queries';
import { DISPOSITION_LABELS, canManagePlan, formatPlanDate, planReviewBody, riskLabel } from '@/lib/plans';
import { Button } from './ui/button';

// Route: /plans. The daily review of discharge plans in rounds (ADR-002 item 8): every stay in progress, per unit,
// not-yet-reviewed-today first. Reviewing records who and when; a passed expected date asks for a delay reason.
const PlanReviewView = () => {
  const navigate = useNavigate();
  const { user } = useAuth();
  const readOnly = useReadOnly();
  const { data: units = [] } = useUnits();
  const [unitId, setUnitId] = useState('');
  const [pendingOnly, setPendingOnly] = useState(true);
  const { data, isLoading } = usePlanReview(unitId);
  const [busy, setBusy] = useState(null);
  const canReview = canManagePlan(user) && !readOnly;

  const review = async (plan) => {
    const body = planReviewBody(plan);
    if (!body) return;
    setBusy(plan.encounter_id);
    try {
      await api.post(`/encounters/${plan.encounter_id}/plan/review`, body);
      await invalidateDischarge();
    } catch (error) {
      toast.error('Could not record the review', { description: errorMessage(error) });
    } finally {
      setBusy(null);
    }
  };

  const groups = (data?.units || []).map(g => ({
    ...g, plans: pendingOnly ? g.plans.filter(p => !p.reviewed_today) : g.plans,
  }));

  return (
    <div className="space-y-6" data-testid="plan-review">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <CalendarCheck className="w-6 h-6 text-blue-400" strokeWidth={1.5} />
          <h1 className="text-2xl font-semibold text-zinc-50" style={{ fontFamily: 'Outfit, sans-serif' }}>
            Discharge plan review
          </h1>
          {data?.today && <span className="text-sm text-zinc-500">{formatPlanDate(data.today)}</span>}
        </div>
        <div className="flex items-center gap-3 text-sm text-zinc-400">
          <select
            aria-label="Unit"
            value={unitId}
            onChange={e => setUnitId(e.target.value)}
            className="h-9 rounded bg-zinc-950 border border-zinc-700 px-2 text-zinc-200"
          >
            <option value="">All my units</option>
            {units.map(u => <option key={u.unit_id} value={u.unit_id}>{u.code} - {u.name}</option>)}
            <option value="unassigned">Unassigned</option>
          </select>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={pendingOnly} onChange={e => setPendingOnly(e.target.checked)}
              data-testid="pending-only" />
            Not reviewed today only
          </label>
        </div>
      </div>

      {isLoading ? (
        <Loader2 className="w-6 h-6 animate-spin text-blue-500" />
      ) : !groups.length ? (
        <p className="text-zinc-500">No patients in house.</p>
      ) : groups.map(group => (
        <div key={group.unit_id || 'unassigned'} className="bg-zinc-900 border border-zinc-800 rounded-lg p-4"
          data-testid={`plan-unit-${group.unit_code || 'unassigned'}`}>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-lg font-medium text-zinc-50">
              {group.unit_code ? `${group.unit_code} - ${group.unit_name}` : group.unit_name}
            </h2>
            <span className="text-sm text-zinc-400">{group.not_reviewed_today} of {group.total} not reviewed today</span>
          </div>
          {!group.plans.length ? (
            <p className="text-sm text-zinc-500">All plans reviewed today.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-zinc-500">
                <tr>
                  <th className="py-1 font-normal">Patient</th>
                  <th className="py-1 font-normal">Room</th>
                  <th className="py-1 font-normal">Expected</th>
                  <th className="py-1 font-normal">Disposition</th>
                  <th className="py-1 font-normal">Readmission risk</th>
                  <th className="py-1 font-normal">Last review</th>
                  <th />
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800">
                {group.plans.map(plan => (
                  <tr key={plan.encounter_id} data-testid={`plan-row-${plan.patient_id}`}>
                    <td className="py-2">
                      <button type="button" className="text-blue-400 hover:underline"
                        onClick={() => navigate(`/patients/${plan.patient_id}`)}>{plan.patient_name}</button>
                    </td>
                    <td className="py-2 text-zinc-300">{plan.room_number}</td>
                    <td className={`py-2 ${plan.expected_date_passed ? 'text-red-400' : 'text-zinc-300'}`}>
                      {formatPlanDate(plan.expected_discharge_date) || <span className="text-amber-400">Not set</span>}
                    </td>
                    <td className="py-2 text-zinc-300">
                      {DISPOSITION_LABELS[plan.planned_disposition] || plan.planned_disposition || '-'}
                    </td>
                    <td className={`py-2 ${plan.readmission_risk?.high_risk ? 'text-red-400' : 'text-zinc-400'}`}
                      data-testid={`plan-risk-${plan.patient_id}`}>
                      {riskLabel(plan.readmission_risk) || '-'}
                    </td>
                    <td className="py-2 text-zinc-500">
                      {plan.plan_reviewed_at
                        ? `${new Date(plan.plan_reviewed_at).toLocaleString()} (${plan.plan_reviewed_by_name || plan.plan_reviewed_by})`
                        : 'Never'}
                    </td>
                    <td className="py-2 text-right">
                      {plan.reviewed_today ? (
                        <span className="text-emerald-400 text-xs">Reviewed today</span>
                      ) : canReview && (
                        <Button size="sm" variant="outline" className="border-zinc-700 text-zinc-300"
                          disabled={busy === plan.encounter_id} onClick={() => review(plan)}
                          data-testid={`plan-review-${plan.patient_id}`}>
                          <Check className="w-4 h-4 mr-1" /> Reviewed
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}
    </div>
  );
};

export default PlanReviewView;
