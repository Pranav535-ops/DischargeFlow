import React from 'react';
import { useMutation } from '@tanstack/react-query';
import { toast } from 'sonner';
import { Sparkles, Loader2, ShieldCheck, AlertCircle, BarChart3, Gauge } from 'lucide-react';
import { api, errorMessage } from '@/lib/api';
import { useAggregates, useOperations } from '@/lib/queries';
import { Button } from './ui/button';

const ROLE_LABEL = { physician: 'Physician', nurse: 'Nurse', pharmacist: 'Pharmacist', admin: 'Admin' };

// A completion-time cell: the average when the group is large enough, else only the count (small groups are
// suppressed server-side so one patient's timeline can't be read off a metric).
const Hours = ({ stats }) => (
  stats?.avg_hours != null
    ? <span>{stats.avg_hours} h <span className="text-zinc-500">(median {stats.median_hours} h, n={stats.n})</span></span>
    : <span className="text-zinc-500">n={stats?.n ?? 0}, too few to average</span>
);

const PRIORITIES = ['high', 'medium', 'low'];

// Throughput, approval wait and task SLA breaches over the last 30 days (GET /api/analytics/operations).
const OperationsPanel = () => {
  const { data: ops, isLoading } = useOperations(30);
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6" data-testid="operations-panel">
      <h2 className="text-lg font-medium text-zinc-50 mb-4 flex items-center gap-2">
        <Gauge className="w-5 h-5 text-blue-400" /> Operations, last {ops?.window_days ?? 30} days
      </h2>
      {isLoading || !ops ? (
        <Loader2 className="w-6 h-6 animate-spin text-blue-500" />
      ) : (
        <div className="grid md:grid-cols-3 gap-6 text-sm">
          <div>
            <h3 className="text-zinc-400 mb-2">Throughput</h3>
            <p className="text-zinc-200" data-testid="ops-throughput">
              {ops.throughput.discharges_completed} completed · {ops.throughput.discharges_started} started
            </p>
            <p className="text-zinc-400 mt-1">Duration: <Hours stats={ops.throughput.discharge_duration} /></p>
          </div>
          <div>
            <h3 className="text-zinc-400 mb-2">Waiting for physician approval</h3>
            <p className="text-zinc-200"><Hours stats={ops.pending_approval.wait} /></p>
            <p className="text-zinc-400 mt-1">
              {ops.pending_approval.waiting_now} waiting now
              {ops.pending_approval.oldest_waiting_hours != null && ` · oldest ${ops.pending_approval.oldest_waiting_hours} h`}
            </p>
            {ops.pending_approval.untracked_completed > 0 && (
              <p className="text-xs text-zinc-500 mt-1">
                {ops.pending_approval.untracked_completed} older discharge(s) not tracked
              </p>
            )}
          </div>
          <div>
            <h3 className="text-zinc-400 mb-2">Task SLA</h3>
            <p className="text-zinc-200">
              {ops.task_sla.completed.breached}/{ops.task_sla.completed.n} completed tasks over target
              {ops.task_sla.completed.breach_rate != null && ` (${Math.round(ops.task_sla.completed.breach_rate * 100)}%)`}
            </p>
            <p className="text-xs text-zinc-500 mt-1">
              Targets: {PRIORITIES.map(p => `${p} ${ops.task_sla.targets_hours[p]} h`).join(', ')}
            </p>
            <table className="w-full mt-2"><tbody>
              {Object.entries(ops.task_sla.open_breaching_by_role).map(([role, counts]) => (
                <tr key={role} className="border-b border-zinc-800/60">
                  <td className="py-1.5 text-zinc-300">{ROLE_LABEL[role] ?? role}</td>
                  <td className="py-1.5 text-amber-400 text-right">
                    {PRIORITIES.filter(p => counts[p]).map(p => `${counts[p]} ${p}`).join(', ')} open, overdue
                  </td>
                </tr>
              ))}
            </tbody></table>
          </div>
        </div>
      )}
    </div>
  );
};

const InsightsView = () => {
  const { data: aggregates, isLoading } = useAggregates();
  const generate = useMutation({
    mutationFn: () => api.post('/analytics/insights').then(res => res.data),
    onError: (error) => toast.error('Could not generate insights', { description: errorMessage(error) }),
  });
  const result = generate.data;
  const agg = result?.aggregates ?? aggregates;

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-zinc-50" style={{ fontFamily: 'Outfit, sans-serif' }}>
            AI Insights
          </h1>
          <p className="text-sm text-zinc-400 mt-1 max-w-2xl">
            Discharge bottlenecks explained from aggregate metrics. Only the counts and durations below are sent to
            the AI; no patient information is included.
          </p>
        </div>
        <Button
          data-testid="generate-insights-btn"
          onClick={() => generate.mutate()}
          disabled={generate.isPending || isLoading}
          className="bg-blue-600 hover:bg-blue-500 shrink-0"
        >
          {generate.isPending ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Sparkles className="w-4 h-4 mr-2" />}
          {result ? 'Refresh insights' : 'Explain trends'}
        </Button>
      </div>

      {result && (
        <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6" data-testid="insights-panel">
          {result.insights ? (
            <>
              <p className="text-lg font-medium text-zinc-50 mb-4">{result.insights.headline}</p>
              <ul className="space-y-4">
                {result.insights.findings.map((f, i) => (
                  <li key={i} className="border-l-2 border-blue-500/50 pl-4">
                    <p className="font-medium text-zinc-200">{f.title}</p>
                    <p className="text-sm text-zinc-400 mt-1">{f.detail}</p>
                    <p className="text-sm text-zinc-300 mt-1">Suggestion: {f.suggestion}</p>
                    <p className="text-xs text-zinc-600 mt-1 font-mono">Based on {f.metric}</p>
                  </li>
                ))}
              </ul>
              {result.rejected_citations?.length > 0 && (
                <p className="mt-4 text-xs text-amber-400 flex items-center gap-1.5">
                  <AlertCircle className="w-3.5 h-3.5" />
                  {result.rejected_citations.length} finding(s) removed because they cited metrics that don't exist.
                </p>
              )}
              <p className="mt-4 text-xs text-zinc-500 flex items-center gap-1.5">
                <ShieldCheck className="w-3.5 h-3.5" />
                {result.generation.model_id} ({result.generation.provider}) · prompt {result.generation.prompt_version}
                {result.generation.provider === 'mock' && ' · offline mock, not a real model'}
              </p>
            </>
          ) : (
            <p className="text-zinc-400 flex items-center gap-2"><AlertCircle className="w-4 h-4" />{result.unavailable}</p>
          )}
        </div>
      )}

      <OperationsPanel />

      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6">
        <h2 className="text-lg font-medium text-zinc-50 mb-4 flex items-center gap-2">
          <BarChart3 className="w-5 h-5 text-blue-400" /> Metrics sent to the AI
        </h2>
        {isLoading || !agg ? (
          <Loader2 className="w-6 h-6 animate-spin text-blue-500" />
        ) : (
          <div className="grid md:grid-cols-2 gap-6 text-sm">
            <div>
              <h3 className="text-zinc-400 mb-2">Task completion time by role</h3>
              <table className="w-full"><tbody>
                {Object.entries(agg.task_completion_time_by_role).map(([role, stats]) => (
                  <tr key={role} className="border-b border-zinc-800/60">
                    <td className="py-1.5 text-zinc-300">{ROLE_LABEL[role] ?? role}</td>
                    <td className="py-1.5 text-zinc-200 text-right"><Hours stats={stats} /></td>
                  </tr>
                ))}
              </tbody></table>
            </div>
            <div>
              <h3 className="text-zinc-400 mb-2">Open tasks by role</h3>
              <table className="w-full"><tbody>
                {Object.entries(agg.open_tasks_by_role).map(([role, stats]) => (
                  <tr key={role} className="border-b border-zinc-800/60">
                    <td className="py-1.5 text-zinc-300">{ROLE_LABEL[role] ?? role}</td>
                    <td className="py-1.5 text-zinc-200 text-right">
                      {stats.n} open{stats.oldest_hours != null && <span className="text-zinc-500"> · oldest {stats.oldest_hours} h</span>}
                    </td>
                  </tr>
                ))}
              </tbody></table>
            </div>
            <div>
              <h3 className="text-zinc-400 mb-2">Discharge duration</h3>
              <p className="text-zinc-200"><Hours stats={agg.discharge_duration} /></p>
            </div>
            <div>
              <h3 className="text-zinc-400 mb-2">AI discharge summaries</h3>
              <p className="text-zinc-200">
                {agg.ai_summaries.approved}/{agg.ai_summaries.generated} approved
                {agg.ai_summaries.avg_edit_similarity != null &&
                  ` · ${Math.round(agg.ai_summaries.avg_edit_similarity * 100)}% similar to the draft on average`}
                {agg.ai_summaries.failed_generations > 0 && ` · ${agg.ai_summaries.failed_generations} failed`}
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default InsightsView;
