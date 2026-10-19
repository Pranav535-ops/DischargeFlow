import React, { useState } from 'react';
import { Loader2, PlugZap, Power } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { invalidateDischarge, keys, queryClient, useDowntimePeriods, useDowntimeStatus } from '@/lib/queries';
import { Button } from './ui/button';
import { Input } from './ui/input';

const refresh = () => Promise.all([
  queryClient.invalidateQueries({ queryKey: ['downtime'] }),
  invalidateDischarge(),
]);

// datetime-local value (browser local time) -> ISO string with offset, as the API requires.
const toIso = (local) => (local ? new Date(local).toISOString() : null);

// Downtime controls for the admin screen (ADR-002 item 6, docs/runbooks/downtime.md): read-only mode on and off,
// and recording an unplanned outage afterwards so the paper work done during it can be back-entered.
const AdminDowntime = () => {
  const { data: status } = useDowntimeStatus();
  const { data: periods = [] } = useDowntimePeriods();
  const [reason, setReason] = useState('');
  const [outage, setOutage] = useState({ started_at: '', ended_at: '', reason: '' });
  const [busy, setBusy] = useState(null);
  const readOnly = status?.read_only === true;

  const run = async (name, call, success) => {
    setBusy(name);
    try {
      await call();
      toast.success(success);
      await refresh();
      return true;
    } catch (error) {
      toast.error('Downtime change failed', { description: errorMessage(error) });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const start = async (e) => {
    e.preventDefault();
    if (!window.confirm('Put DischargeFlow in read-only mode? Every write will be refused until you end it.')) return;
    if (await run('start', () => api.post('/downtime/start', { reason }), 'Read-only mode is on')) setReason('');
  };
  const end = () => run('end', () => api.post('/downtime/end'), 'Read-only mode is off');
  const record = async (e) => {
    e.preventDefault();
    const body = { ...outage, started_at: toIso(outage.started_at), ended_at: toIso(outage.ended_at) };
    if (await run('outage', () => api.post('/downtime/periods', body), 'Outage recorded')) {
      setOutage({ started_at: '', ended_at: '', reason: '' });
      await queryClient.invalidateQueries({ queryKey: keys.downtimePeriods });
    }
  };

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 space-y-4" data-testid="downtime-card">
      <div className="flex items-center gap-2">
        <Power className="w-4 h-4 text-zinc-400" />
        <h2 className="text-lg font-medium text-zinc-50">Downtime</h2>
        <span className={`ml-2 px-2 py-0.5 rounded text-xs ${readOnly ? 'bg-amber-500/15 text-amber-300' : 'bg-emerald-500/10 text-emerald-400'}`}>
          {readOnly ? 'Read-only' : 'Normal'}
        </span>
      </div>
      <p className="text-xs text-zinc-500">
        Read-only mode refuses every change on every server at once; reads, the census and sign-in keep working.
        Staff work on paper and enter it afterwards with the time it was done. See the downtime runbook.
      </p>

      {status?.forced && (
        <p className="text-sm text-amber-300">READ_ONLY_MODE is set in the deployment configuration, so writes stay off until it is removed.</p>
      )}
      {status?.period ? (
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-sm text-zinc-300">
            On since {new Date(status.period.started_at).toLocaleString()}: {status.period.reason}
          </span>
          <Button data-testid="end-downtime" onClick={end} disabled={busy === 'end'} className="bg-emerald-600 hover:bg-emerald-500">
            {busy === 'end' && <Loader2 className="w-4 h-4 mr-2 animate-spin" />} End read-only mode
          </Button>
        </div>
      ) : (
        <form onSubmit={start} className="flex flex-wrap gap-2">
          <Input
            data-testid="downtime-reason"
            value={reason}
            onChange={e => setReason(e.target.value)}
            placeholder="Shown to everyone, e.g. Database maintenance until 02:00 (no patient details)"
            minLength={3}
            maxLength={300}
            required
            disabled={readOnly}
            className="flex-1 min-w-64 bg-zinc-950 border-zinc-700"
          />
          <Button data-testid="start-downtime" type="submit" disabled={busy === 'start' || readOnly} className="bg-amber-600 hover:bg-amber-500">
            {busy === 'start' && <Loader2 className="w-4 h-4 mr-2 animate-spin" />} Start read-only mode
          </Button>
        </form>
      )}

      <form onSubmit={record} className="space-y-2 border-t border-zinc-800 pt-3">
        <div className="flex items-center gap-2 text-sm text-zinc-300">
          <PlugZap className="w-4 h-4 text-zinc-400" /> Record an unplanned outage (after it is over)
        </div>
        <div className="flex flex-wrap gap-2">
          <Input type="datetime-local" aria-label="Outage start" required value={outage.started_at} disabled={readOnly}
                 onChange={e => setOutage({ ...outage, started_at: e.target.value })} className="w-56 bg-zinc-950 border-zinc-700" />
          <Input type="datetime-local" aria-label="Outage end" required value={outage.ended_at} disabled={readOnly}
                 onChange={e => setOutage({ ...outage, ended_at: e.target.value })} className="w-56 bg-zinc-950 border-zinc-700" />
          <Input value={outage.reason} placeholder="What happened" required minLength={3} maxLength={300} disabled={readOnly}
                 onChange={e => setOutage({ ...outage, reason: e.target.value })} className="flex-1 min-w-48 bg-zinc-950 border-zinc-700" />
          <Button type="submit" variant="outline" disabled={busy === 'outage' || readOnly} className="border-zinc-700 text-zinc-300">
            Record outage
          </Button>
        </div>
      </form>

      {periods.length > 0 && (
        <table className="w-full text-xs text-zinc-400">
          <thead>
            <tr className="text-left text-zinc-500"><th className="py-1">Kind</th><th>Start</th><th>End</th><th>Reason</th></tr>
          </thead>
          <tbody>
            {periods.slice(0, 10).map(p => (
              <tr key={p.downtime_id} className="border-t border-zinc-800">
                <td className="py-1">{p.kind === 'outage' ? 'Outage' : 'Read-only'}</td>
                <td>{new Date(p.started_at).toLocaleString()}</td>
                <td>{p.ended_at ? new Date(p.ended_at).toLocaleString() : 'ongoing'}</td>
                <td className="text-zinc-300">{p.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};

export default AdminDowntime;
