import React, { useState } from 'react';
import { ShieldAlert, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { invalidateDischarge } from '@/lib/queries';
import { Button } from './ui/button';

const MIN_REASON = 10;

// Shown when the API answers 403 with `break_glass: true`: the patient exists but is outside the user's units
// (UNIT_SCOPED_ACCESS, ADR-002 item 3). Opening it records the reason, audits the access and tells the admins.
const BreakGlassPanel = ({ patientId, onBack }) => {
  const [reason, setReason] = useState('');
  const [sending, setSending] = useState(false);
  const ready = reason.trim().length >= MIN_REASON;

  const submit = async (e) => {
    e.preventDefault();
    setSending(true);
    try {
      await api.post(`/patients/${patientId}/break-glass`, { reason: reason.trim() });
      toast.warning('Emergency access granted', { description: 'This access is recorded and reviewed.' });
      await invalidateDischarge();
    } catch (error) {
      toast.error('Access not granted', { description: errorMessage(error) });
    } finally {
      setSending(false);
    }
  };

  return (
    <form onSubmit={submit} data-testid="break-glass-panel"
          className="max-w-lg mx-auto mt-12 bg-zinc-900 border border-amber-500/30 rounded-lg p-6 space-y-4">
      <div className="flex items-center gap-3">
        <ShieldAlert className="w-6 h-6 text-amber-400" strokeWidth={1.5} />
        <h1 className="text-lg font-semibold text-zinc-50">This patient is outside your units</h1>
      </div>
      <p className="text-sm text-zinc-400">
        You can open the record for 8 hours if you need it for this patient&apos;s care. Your name, the time and your
        reason are recorded, and administrators review every emergency access.
      </p>
      <label htmlFor="break-glass-reason" className="block text-xs text-zinc-500">Reason (at least {MIN_REASON} characters)</label>
      <textarea id="break-glass-reason" data-testid="break-glass-reason" value={reason} maxLength={500} rows={3}
                onChange={(e) => setReason(e.target.value)}
                className="w-full bg-zinc-800 border border-zinc-700 rounded p-2 text-sm text-zinc-100 focus:outline-none focus:ring-1 focus:ring-amber-500" />
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onBack} className="text-zinc-400">Back to patients</Button>
        <Button type="submit" data-testid="break-glass-submit" disabled={!ready || sending}
                className="bg-amber-600 hover:bg-amber-500">
          {sending && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
          Open record
        </Button>
      </div>
    </form>
  );
};

export default BreakGlassPanel;
