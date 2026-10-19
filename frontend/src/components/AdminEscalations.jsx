import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { BellRing, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { keys, queryClient, useEscalations, useReadOnly } from '@/lib/queries';
import { Button } from './ui/button';

const PAGE_LABELS = {
  not_required: 'not paged (policy)',
  disabled: 'no pager configured',
  pending: 'waiting',
  sent: 'paged',
  failed: 'page failed',
  cancelled: 'cancelled',
};

const minutesSince = (iso) => Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));

// Escalation of overdue tasks for the admin screen (ADR-002 item 12): what is escalated now, the policy, and a
// manual run. Rows carry ids and codes only; the link opens the task's workflow, where the usual access rules apply.
const AdminEscalations = () => {
  const { data } = useEscalations();
  const readOnly = useReadOnly();
  const [running, setRunning] = useState(false);
  const rows = data?.escalations ?? [];

  const runNow = async () => {
    setRunning(true);
    try {
      const { data: result } = await api.post('/escalations/run');
      const r = result.tenants[0] ?? {};
      if (r.skipped) toast.info(`Escalation run skipped (${r.skipped.replace('_', ' ')})`);
      else toast.success(`Escalated ${r.escalated}, resolved ${r.resolved}, paged ${r.paged}`);
      await queryClient.invalidateQueries({ queryKey: keys.escalations });
    } catch (error) {
      toast.error('Escalation run failed', { description: errorMessage(error) });
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 space-y-3" data-testid="escalations-card">
      <div className="flex items-center gap-2">
        <BellRing className="w-4 h-4 text-zinc-400" />
        <h2 className="text-lg font-medium text-zinc-50">Escalations</h2>
        <span className="ml-2 px-2 py-0.5 rounded text-xs bg-zinc-800 text-zinc-300">{rows.length} active</span>
        <Button data-testid="run-escalations" size="sm" variant="outline" onClick={runNow} disabled={running || readOnly}
                className="ml-auto border-zinc-700 text-zinc-300">
          {running && <Loader2 className="w-4 h-4 mr-2 animate-spin" />} Run now
        </Button>
      </div>
      {data && (
        <p className="text-xs text-zinc-500">
          Overdue tasks go first to the assignee, then to the supervising role
          ({Object.entries(data.policy).map(([p, v]) => `${p}: ${v.sla_hours} h${v.steps.length > 1 ? ` + ${v.steps[1].after_breach_hours} h` : ''}`).join(', ')}).
          {' '}Pager: {data.pager === 'none' ? 'in-app only' : data.pager}.
          {' '}{data.scheduled ? 'Runs on a schedule.' : 'No schedule configured (CRON_SECRET unset): runs only when started.'}
        </p>
      )}
      {rows.length > 0 && (
        <table className="w-full text-xs text-zinc-400">
          <thead>
            <tr className="text-left text-zinc-500">
              <th className="py-1">Task</th><th>Priority</th><th>Unit</th><th>Level</th><th>Told</th><th>Page</th><th>Since</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(e => (
              <tr key={e.escalation_id} className="border-t border-zinc-800" data-testid="escalation-row">
                <td className="py-1"><Link to={e.link} className="text-blue-400 hover:underline">{e.task_id}</Link> <span className="text-zinc-500">{e.category}</span></td>
                <td>{e.priority_name}</td>
                <td>{e.unit_code || 'unassigned'}</td>
                <td>{e.level}</td>
                <td className={e.recipients === 0 ? 'text-rose-400' : ''}>
                  {e.target_user_id ? 'assignee' : e.target_role} ({e.recipients})
                </td>
                <td>{PAGE_LABELS[e.page_status] ?? e.page_status}</td>
                <td>{minutesSince(e.escalated_at)} min</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};

export default AdminEscalations;
