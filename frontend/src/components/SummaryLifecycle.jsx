import React, { useState } from 'react';
import { History, Loader2, PenLine, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { Button } from './ui/button';

const when = (iso) => (iso ? new Date(iso).toLocaleString() : '');

// Sign-by deadline of an unsigned summary (ADR-002 item 13), shown next to the summary heading.
export const SignByBadge = ({ workflow }) => {
  if (!workflow?.summary_sign_by || workflow.discharge_summary?.approved) return null;
  return workflow.summary_overdue ? (
    <span data-testid="summary-overdue" className="text-sm text-red-400">
      Signature overdue (due {when(workflow.summary_sign_by)})
    </span>
  ) : (
    <span data-testid="summary-sign-by" className="text-sm text-zinc-400">
      Sign by {when(workflow.summary_sign_by)}
    </span>
  );
};

// A signed summary: who signed which version and when, the version history, the addenda, and the form that adds
// one. The signed text itself can't change; anything after signature is an addendum, signed by its author.
const SummaryLifecycle = ({ summary, canSign, readOnly, onChanged }) => {
  const [text, setText] = useState('');
  const [saving, setSaving] = useState(false);
  const signed = summary.signed_version;
  const addenda = summary.addenda || [];

  const addAddendum = async (e) => {
    e.preventDefault();
    if (!text.trim()) return;
    setSaving(true);
    try {
      await api.post(`/ai/summaries/${summary.summary_id}/addenda`, { content: text });
      await onChanged();
      setText('');
      toast.success('Addendum signed', { description: 'Added to the signed summary.' });
    } catch (error) {
      toast.error('Failed to add addendum', { description: errorMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-4 space-y-4" data-testid="summary-lifecycle">
      {signed && (
        <p className="flex items-center gap-2 text-xs text-zinc-400" data-testid="signed-version">
          <ShieldCheck className="w-4 h-4 text-emerald-400" />
          Version {signed.version} signed by {signed.signed_by} on {when(signed.signed_at)}
          {signed.content_sha256 && <span className="font-mono text-zinc-500">· sha256 {signed.content_sha256.slice(0, 12)}</span>}
          {signed.hash_verified === false && <span className="text-red-400">· text does not match its signature</span>}
          {summary.signed_late && <span className="text-amber-400">· signed after the sign-by deadline</span>}
        </p>
      )}

      {summary.versions?.length > 0 && (
        <div data-testid="version-history">
          <h3 className="flex items-center gap-2 text-sm font-medium text-zinc-300 mb-2">
            <History className="w-4 h-4" /> Version history
          </h3>
          <ol className="text-xs text-zinc-400 space-y-1">
            <li>Draft created {when(summary.created_at)}</li>
            {summary.versions.map(v => (
              <li key={v.version_id}>Version {v.version} signed by {v.signed_by} on {when(v.signed_at)}</li>
            ))}
            {addenda.map((a, i) => (
              <li key={a.addendum_id}>Addendum {i + 1} signed by {a.signed_by} on {when(a.signed_at)}</li>
            ))}
          </ol>
        </div>
      )}

      {addenda.length > 0 && (
        <div className="space-y-3" data-testid="addenda">
          {addenda.map((a, i) => (
            <div key={a.addendum_id} className="rounded-md border border-zinc-800 bg-zinc-950 p-3">
              <p className="text-xs text-zinc-500 mb-1">
                Addendum {i + 1} · {a.signed_by} · {when(a.signed_at)}
              </p>
              <pre className="whitespace-pre-wrap text-sm text-zinc-300 font-sans leading-relaxed">{a.content}</pre>
            </div>
          ))}
        </div>
      )}

      {canSign && (
        <form onSubmit={addAddendum} className="space-y-2">
          <label htmlFor="addendum-text" className="text-sm text-zinc-300">Add an addendum</label>
          <textarea
            id="addendum-text"
            data-testid="addendum-input"
            value={text}
            onChange={e => setText(e.target.value)}
            rows={4}
            maxLength={20000}
            disabled={readOnly}
            placeholder="Late results, corrections or follow-up. The signed summary stays as it is; this is added below it and signed by you."
            className="w-full rounded-md bg-zinc-950 border border-zinc-700 p-3 text-sm text-zinc-200 focus:outline-none focus:border-blue-500 placeholder-zinc-600"
          />
          <Button
            type="submit"
            data-testid="add-addendum-btn"
            disabled={saving || readOnly || !text.trim()}
            className="bg-blue-600 hover:bg-blue-500"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <PenLine className="w-4 h-4 mr-2" />}
            Sign Addendum
          </Button>
        </form>
      )}
    </div>
  );
};

export default SummaryLifecycle;
