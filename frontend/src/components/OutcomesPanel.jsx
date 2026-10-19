import React, { useState } from 'react';
import { Loader2, Target } from 'lucide-react';
import { percent } from '@/lib/capacity';
import { useOutcomes } from '@/lib/queries';

const WINDOWS = [30, 90, 180, 365];

// A rate with its count, or the count alone when the group is under the minimum (suppressed server-side).
const Share = ({ value }) => (
  value?.rate != null
    ? <span>{percent(value.rate)} <span className="text-zinc-500">({value.count}/{value.n})</span></span>
    : <span className="text-zinc-500">{value?.count ?? 0}/{value?.n ?? 0}, too few for a rate</span>
);
const Days = ({ stats, signed }) => (
  stats?.avg_days != null
    ? <span>{signed && stats.avg_days > 0 ? '+' : ''}{stats.avg_days} d <span className="text-zinc-500">(median {stats.median_days}, n={stats.n})</span></span>
    : <span className="text-zinc-500">n={stats?.n ?? 0}, too few to average</span>
);

const OutcomeRows = ({ group }) => (
  <>
    <td className="py-2 pr-3 text-zinc-200">{group.discharges}</td>
    <td className="py-2 pr-3"><Days stats={group.length_of_stay_days} /></td>
    <td className="py-2 pr-3"><Days stats={group.vs_expected_date} signed /></td>
    <td className="py-2 pr-3"><Share value={group.expected_date_accuracy.final_exact} /></td>
    <td className="py-2 pr-3"><Share value={group.expected_date_accuracy.first_exact} /></td>
    <td className="py-2 pr-3"><Share value={group.before_noon} /></td>
    <td className="py-2 pr-3"><Share value={group.readmitted_within_30_days} /></td>
  </>
);

// Outcome analytics (ADR-002 item 19, GET /api/analytics/outcomes): aggregate only, by unit.
const OutcomesPanel = () => {
  const [days, setDays] = useState(90);
  const { data, isLoading } = useOutcomes(days);
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6" data-testid="outcomes-panel">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <h2 className="text-lg font-medium text-zinc-50 flex items-center gap-2">
          <Target className="w-5 h-5 text-blue-400" /> Outcomes of discharges, last {days} days
        </h2>
        <select aria-label="Outcome window" value={days} onChange={e => setDays(Number(e.target.value))}
          className="h-9 rounded bg-zinc-950 border border-zinc-700 px-2 text-zinc-200 text-sm">
          {WINDOWS.map(d => <option key={d} value={d}>{d} days</option>)}
        </select>
      </div>
      {isLoading || !data?.overall ? (
        <Loader2 className="w-6 h-6 animate-spin text-blue-500" />
      ) : (
        <div className="space-y-6 text-sm">
          <div className="overflow-x-auto">
            <table className="w-full" data-testid="outcomes-table">
              <thead>
                <tr className="text-left text-zinc-500">
                  <th className="font-normal pr-3">Unit</th>
                  <th className="font-normal pr-3">Discharges</th>
                  <th className="font-normal pr-3">Length of stay</th>
                  <th className="font-normal pr-3">Against expected date</th>
                  <th className="font-normal pr-3">Final date exact</th>
                  <th className="font-normal pr-3">First date exact</th>
                  <th className="font-normal pr-3">Before noon</th>
                  <th className="font-normal pr-3">Readmitted in 30 d</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-t border-zinc-800 font-medium" data-testid="outcomes-all">
                  <td className="py-2 pr-3 text-zinc-50">All units</td>
                  <OutcomeRows group={data.overall} />
                </tr>
                {Object.entries(data.by_unit).map(([unit, group]) => (
                  <tr key={unit} className="border-t border-zinc-800" data-testid={`outcomes-unit-${unit}`}>
                    <td className="py-2 pr-3 text-zinc-300">{unit === 'unassigned' ? 'Unassigned' : unit}</td>
                    <OutcomeRows group={group} />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div>
            <h3 className="text-zinc-400 mb-2">Delay reasons ({data.delay_reasons.recorded})</h3>
            {!data.delay_reasons.pareto.length ? (
              <p className="text-zinc-500">No delay reasons recorded.</p>
            ) : (
              <ul className="space-y-1" data-testid="delay-pareto">
                {data.delay_reasons.pareto.map(row => (
                  <li key={row.code} className="flex items-center gap-3">
                    <span className="w-28 text-zinc-300 capitalize">{row.code}</span>
                    <span className="flex-1 h-2 bg-zinc-800 rounded">
                      <span className="block h-2 bg-blue-500 rounded" style={{ width: `${row.share * 100}%` }} />
                    </span>
                    <span className="w-36 text-right text-zinc-400">
                      {row.n} · {percent(row.share)} (cum. {percent(row.cumulative_share)})
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <p className="text-xs text-zinc-500">
            Groups under {data.min_group_size} show counts only. Attributed {data.notes.attribution}.
            First expected date {data.notes.first_expected_date}. Readmission: {data.notes.readmission}.
          </p>
        </div>
      )}
    </div>
  );
};

export default OutcomesPanel;
