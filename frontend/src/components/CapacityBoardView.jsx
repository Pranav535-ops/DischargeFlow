import React, { useState } from 'react';
import { BedDouble, Loader2, Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '../context/AuthContext';
import { api, errorMessage } from '@/lib/api';
import { BOARD_HOURS, NEXT_BED_STATUS, canManageBeds, percent } from '@/lib/capacity';
import { queryClient, useCapacityBoard, useReadOnly } from '@/lib/queries';
import { Button } from './ui/button';

const BED_LABEL = { dirty: 'Dirty', cleaning: 'Cleaning', available: 'Available' };
const BED_STYLE = {
  dirty: 'bg-red-500/15 text-red-300 border-red-500/30',
  cleaning: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  available: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
};

const unitLabel = (u) => (u.unit_code ? `${u.unit_code} - ${u.unit_name}` : u.unit_name);
const clock = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '');
const duration = (minutes) => (minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`);

const Tile = ({ label, value, hint, testId }) => (
  <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4" data-testid={testId}>
    <p className="text-xs uppercase tracking-wide text-zinc-500">{label}</p>
    <p className="text-2xl font-semibold text-zinc-50 mt-1">{value}</p>
    {hint && <p className="text-xs text-zinc-500 mt-1">{hint}</p>}
  </div>
);

// Route: /capacity. Expected discharges by unit and hour, discharge before noon, and the bed-to-cleaning handoff
// (ADR-002 item 17). Counts and bed locations only; unit-scoped users see their own units.
const CapacityBoardView = () => {
  const { user } = useAuth();
  const readOnly = useReadOnly();
  const [day, setDay] = useState('');
  const { data, isLoading } = useCapacityBoard(day);
  const [busy, setBusy] = useState(null);
  const canAdvance = canManageBeds(user) && !readOnly;

  const advance = async (bed) => {
    const status = NEXT_BED_STATUS[bed.status];
    setBusy(bed.turnover_id);
    try {
      await api.put(`/beds/${bed.turnover_id}`, { status });
      await queryClient.invalidateQueries({ queryKey: ['patients', 'capacity'] });
    } catch (error) {
      toast.error('Could not update the bed', { description: errorMessage(error) });
    } finally {
      setBusy(null);
    }
  };

  const totals = data?.totals;
  const units = data?.units || [];
  const isToday = data && data.date === data.today;

  return (
    <div className="space-y-6" data-testid="capacity-board">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <BedDouble className="w-6 h-6 text-blue-400" strokeWidth={1.5} />
          <h1 className="text-2xl font-semibold text-zinc-50" style={{ fontFamily: 'Outfit, sans-serif' }}>
            Capacity board
          </h1>
          {data?.timezone && <span className="text-sm text-zinc-500">{data.date} · {data.timezone}</span>}
        </div>
        <label className="flex items-center gap-2 text-sm text-zinc-400">
          Day
          <input type="date" aria-label="Day" value={day || data?.today || ''}
            onChange={e => setDay(e.target.value === data?.today ? '' : e.target.value)}
            className="h-9 rounded bg-zinc-950 border border-zinc-700 px-2 text-zinc-200" />
        </label>
      </div>

      {isLoading || !data ? (
        <Loader2 className="w-6 h-6 animate-spin text-blue-500" />
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-4">
            <Tile label="In house" value={totals.in_house} testId="tile-in-house" />
            <Tile label="Expected" value={totals.expected} testId="tile-expected"
              hint={`${totals.unscheduled} without a time · ${totals.expected_tomorrow} tomorrow`} />
            <Tile label="Past expected date" value={totals.past_expected_date} testId="tile-past" />
            <Tile label="Discharged" value={totals.discharged} testId="tile-discharged"
              hint={`${totals.discharged_before_noon} before noon`} />
            <Tile label="Before noon, 7 days" value={percent(totals.before_noon_7d.rate) ?? '-'}
              testId="tile-before-noon"
              hint={`${totals.before_noon_7d.before_noon} of ${totals.before_noon_7d.n} discharges`} />
            <Tile label="Beds to clean" value={totals.beds_dirty + totals.beds_cleaning} testId="tile-beds"
              hint={`${totals.beds_dirty} dirty · ${totals.beds_cleaning} cleaning`} />
          </div>

          <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 overflow-x-auto">
            <h2 className="text-lg font-medium text-zinc-50 mb-3">Expected discharges by hour</h2>
            {!units.length ? (
              <p className="text-zinc-500 text-sm">No units to show.</p>
            ) : (
              <table className="w-full text-sm" data-testid="hour-grid">
                <thead>
                  <tr className="text-zinc-500">
                    <th className="text-left font-normal pr-3 py-1">Unit</th>
                    {BOARD_HOURS.map(h => <th key={h} className="font-normal px-1">{String(h).padStart(2, '0')}</th>)}
                    <th className="font-normal px-2">No time</th>
                    <th className="font-normal px-2">Expected</th>
                    <th className="font-normal px-2">Discharged</th>
                    <th className="font-normal px-2">Before noon (7 d)</th>
                  </tr>
                </thead>
                <tbody>
                  {units.map(u => (
                    <tr key={u.unit_id || 'unassigned'} className="border-t border-zinc-800"
                      data-testid={`hour-row-${u.unit_code || 'unassigned'}`}>
                      <td className="text-zinc-200 pr-3 py-2 whitespace-nowrap">{unitLabel(u)}</td>
                      {BOARD_HOURS.map(h => {
                        const n = u.expected_by_hour[String(h)] || 0;
                        return (
                          <td key={h} className="text-center px-1">
                            {n ? <span className="inline-block min-w-6 rounded bg-blue-500/20 text-blue-200 px-1">{n}</span>
                              : <span className="text-zinc-700">·</span>}
                          </td>
                        );
                      })}
                      <td className="text-center text-zinc-300">{u.unscheduled}</td>
                      <td className="text-center text-zinc-100">{u.expected}</td>
                      <td className="text-center text-zinc-100">{u.discharged}</td>
                      <td className="text-center text-zinc-300">
                        {percent(u.before_noon_7d.rate) ?? '-'}
                        <span className="text-zinc-500"> ({u.before_noon_7d.before_noon}/{u.before_noon_7d.n})</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="text-xs text-zinc-500 mt-3">
              The hour comes from the discharge workflow's estimated time; plans without one are counted under
              &quot;No time&quot;.
            </p>
          </div>

          <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
            <h2 className="text-lg font-medium text-zinc-50 mb-3 flex items-center gap-2">
              <Sparkles className="w-5 h-5 text-blue-400" /> Bed turnover
            </h2>
            {!data.beds.length ? (
              <p className="text-zinc-500 text-sm" data-testid="no-beds">No beds waiting to be cleaned.</p>
            ) : (
              <ul className="divide-y divide-zinc-800" data-testid="bed-list">
                {data.beds.map(bed => {
                  const unit = units.find(u => u.unit_id === bed.unit_id);
                  const next = NEXT_BED_STATUS[bed.status];
                  return (
                    <li key={bed.turnover_id} className="flex flex-wrap items-center gap-3 py-2"
                      data-testid={`bed-${bed.turnover_id}`}>
                      <span className={`text-xs border rounded px-2 py-0.5 ${BED_STYLE[bed.status]}`}>
                        {BED_LABEL[bed.status]}
                      </span>
                      <span className="text-zinc-100 font-medium">Room {bed.room_number}</span>
                      <span className="text-zinc-500 text-sm">{unit ? unitLabel(unit) : 'Unassigned'}</span>
                      <span className="text-zinc-400 text-sm">
                        dirty since {clock(bed.dirty_at)}
                        {bed.cleaning_started_at && ` · cleaning from ${clock(bed.cleaning_started_at)}`}
                        {bed.available_at && ` · available at ${clock(bed.available_at)}`}
                        {` · ${duration(bed.minutes_out_of_service)} out of service`}
                      </span>
                      {next && canAdvance && isToday && (
                        <Button size="sm" variant="outline" className="ml-auto" disabled={busy === bed.turnover_id}
                          onClick={() => advance(bed)}>
                          {next === 'cleaning' ? 'Start cleaning' : 'Mark available'}
                        </Button>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
};

export default CapacityBoardView;
