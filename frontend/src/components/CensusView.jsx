import React, { useState } from 'react';
import { Download, Loader2, Printer, RefreshCw } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { useCensus, useUnits } from '@/lib/queries';
import { Button } from './ui/button';

const PRIORITY = { 1: 'High', 2: 'Medium', 3: 'Low' };
const DISCHARGE = { not_started: 'Not started', in_progress: 'In progress', pending_approval: 'Pending approval' };

// Route: /census. The discharge census for one unit or all of them (ADR-002 item 6): printed at the start of a
// downtime so the unit can carry on from paper. Every load and download is an audited PHI read, so it loads once and
// refreshes only when asked.
const CensusView = () => {
  const { data: units = [] } = useUnits();
  const [unitId, setUnitId] = useState('');
  const [loadedAt, setLoadedAt] = useState(() => Date.now());
  const { data: census, isLoading, isError, error } = useCensus(unitId, loadedAt);
  const [downloading, setDownloading] = useState(false);

  const downloadCsv = async () => {
    setDownloading(true);
    try {
      const res = await api.get('/census', {
        params: { format: 'csv', ...(unitId ? { unit_id: unitId } : {}) },
        responseType: 'blob',
      });
      const match = /filename="([^"]+)"/.exec(res.headers?.['content-disposition'] || '');
      const url = URL.createObjectURL(res.data);
      const a = document.createElement('a');
      a.href = url;
      a.download = match ? match[1] : 'discharge-census.csv';
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast.error('Could not download the census', { description: errorMessage(err) });
    } finally {
      setDownloading(false);
    }
  };

  const unitLabel = census?.unit?.code
    ? `${census.unit.code} · ${census.unit.name}`
    : census?.unit?.unit_id === 'unassigned' ? 'Patients with no unit' : 'All units';

  return (
    <div className="space-y-6 census-page">
      <div className="flex flex-wrap items-end justify-between gap-4 print:hidden">
        <div>
          <h1 className="text-2xl font-semibold text-zinc-50" style={{ fontFamily: 'Outfit, sans-serif' }}>
            Discharge Census
          </h1>
          <p className="text-zinc-400 text-sm">Patients in hospital, their discharge status and open tasks. Print it before or at the start of a downtime.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            data-testid="census-unit"
            value={unitId}
            onChange={e => { setUnitId(e.target.value); setLoadedAt(Date.now()); }}
            className="h-9 rounded-md bg-zinc-900 border border-zinc-700 px-3 text-sm text-zinc-200"
          >
            <option value="">All units</option>
            {units.map(u => <option key={u.unit_id} value={u.unit_id}>{u.code} · {u.name}</option>)}
            <option value="unassigned">No unit</option>
          </select>
          <Button variant="outline" className="border-zinc-700 text-zinc-300" onClick={() => setLoadedAt(Date.now())}>
            <RefreshCw className="w-4 h-4 mr-2" /> Refresh
          </Button>
          <Button variant="outline" className="border-zinc-700 text-zinc-300" onClick={downloadCsv} disabled={downloading}>
            {downloading ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Download className="w-4 h-4 mr-2" />} CSV
          </Button>
          <Button data-testid="census-print" className="bg-blue-600 hover:bg-blue-500" onClick={() => window.print()} disabled={!census}>
            <Printer className="w-4 h-4 mr-2" /> Print
          </Button>
        </div>
      </div>

      {isLoading && <div className="flex justify-center h-32 items-center"><Loader2 className="w-8 h-8 animate-spin text-blue-500" /></div>}
      {isError && <p className="text-red-400">{errorMessage(error, 'Could not load the census.')}</p>}

      {census && (
        <div className="census-sheet rounded-lg border border-zinc-800 bg-zinc-900 p-6 print:border-0 print:bg-white print:p-0 print:text-black">
          <div className="flex flex-wrap items-baseline justify-between gap-2 mb-4">
            <h2 className="text-lg font-medium text-zinc-50 print:text-black">Discharge census: {unitLabel}</h2>
            <p className="text-xs text-zinc-400 print:text-black">
              Printed {new Date(census.generated_at).toLocaleString()} · {census.totals.patients} patients ·{' '}
              {census.totals.in_discharge} in discharge · {census.totals.open_tasks} open tasks · Confidential: contains patient information
            </p>
          </div>
          {census.patients.length === 0 ? (
            <p className="text-zinc-400">No patients in hospital for this selection.</p>
          ) : (
            <table className="w-full text-sm border-collapse" data-testid="census-table">
              <thead>
                <tr className="text-left text-xs uppercase tracking-wide text-zinc-500 print:text-black border-b border-zinc-700 print:border-black">
                  <th className="py-2 pr-3">Unit / room</th>
                  <th className="py-2 pr-3">Patient</th>
                  <th className="py-2 pr-3">Attending</th>
                  <th className="py-2 pr-3">Discharge</th>
                  <th className="py-2">Open tasks (tick when done on paper, with time and initials)</th>
                </tr>
              </thead>
              <tbody>
                {census.patients.map(p => (
                  <tr key={p.patient_id} className="align-top border-b border-zinc-800 print:border-zinc-400 break-inside-avoid">
                    <td className="py-2 pr-3 text-zinc-300 print:text-black whitespace-nowrap">
                      {p.unit_code || '-'} / {p.room_number}
                    </td>
                    <td className="py-2 pr-3 text-zinc-200 print:text-black">
                      <div className="font-medium">{p.name}</div>
                      <div className="text-xs text-zinc-500 print:text-black">
                        {p.mrn ? `MRN ${p.mrn} · ` : ''}DOB {p.date_of_birth} · admitted {p.admission_date}
                      </div>
                    </td>
                    <td className="py-2 pr-3 text-zinc-300 print:text-black">{p.attending_physician}</td>
                    <td className="py-2 pr-3 text-zinc-300 print:text-black">
                      <div>{DISCHARGE[p.discharge_status] || p.discharge_status}</div>
                      {p.estimated_discharge_time && (
                        <div className="text-xs text-zinc-500 print:text-black">Est. {new Date(p.estimated_discharge_time).toLocaleString()}</div>
                      )}
                      {p.workflow_id && (
                        <div className="text-xs text-zinc-500 print:text-black">Summary {p.summary_approved ? 'approved' : 'not approved'}</div>
                      )}
                    </td>
                    <td className="py-2 text-zinc-300 print:text-black">
                      {p.open_tasks.length === 0 ? <span className="text-zinc-500 print:text-black">None</span> : (
                        <ul className="space-y-1">
                          {p.open_tasks.map(t => (
                            <li key={t.task_id} className="flex gap-2">
                              <span className="inline-block w-3 h-3 mt-1 border border-zinc-500 print:border-black shrink-0" aria-hidden />
                              <span>
                                {t.title} <span className="text-xs text-zinc-500 print:text-black">
                                  ({t.assigned_to_name || t.assigned_role}, {PRIORITY[t.priority] || t.priority}
                                  {t.status === 'in_progress' ? ', in progress' : ''})
                                </span>
                              </span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
};

export default CensusView;
