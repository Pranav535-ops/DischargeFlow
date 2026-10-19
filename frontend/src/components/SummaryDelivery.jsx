import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { Loader2, RotateCcw, Send, X } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { keys, queryClient, useDeliveries, useDeliveryEndpoints, useReadOnly, useRecipients, useWorkflowDeliveries } from '@/lib/queries';
import { Button } from './ui/button';

const when = (iso) => (iso ? new Date(iso).toLocaleString() : '');

export const STATUS_LABELS = {
  queued: 'queued',
  sent: 'sent, awaiting acknowledgement',
  acknowledged: 'delivered',
  failed: 'failed',
  cancelled: 'cancelled',
};
const STATUS_COLORS = {
  queued: 'text-zinc-400',
  sent: 'text-blue-400',
  acknowledged: 'text-emerald-400',
  failed: 'text-red-400',
  cancelled: 'text-zinc-500',
};
const FORMAT_LABELS = { fhir_bundle: 'FHIR document', ccda: 'C-CDA' };

export const DeliveryStatus = ({ delivery }) => (
  <span data-testid="delivery-status" className={STATUS_COLORS[delivery.status] ?? 'text-zinc-400'}>
    {STATUS_LABELS[delivery.status] ?? delivery.status}
    {delivery.status === 'queued' && delivery.attempts > 0 && ` (attempt ${delivery.attempts + 1} at ${when(delivery.next_attempt_at)})`}
    {delivery.last_error && ` · ${delivery.last_error}`}
  </span>
);

// Who gets this stay's signed discharge summary (ADR-002 item 16), and what happened to each delivery. Adding a
// recipient after signature sends the summary as it stands now.
export const SummaryRecipients = ({ workflow, canManage }) => {
  const encounterId = workflow?.encounter_id;
  const readOnly = useReadOnly();
  const { data: recipients = [] } = useRecipients(encounterId);
  const { data: endpoints = [] } = useDeliveryEndpoints();
  const { data: deliveries = [] } = useWorkflowDeliveries(workflow?.workflow_id);
  const [choice, setChoice] = useState('');
  const [busy, setBusy] = useState(false);
  if (!encounterId) return null;

  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: keys.recipients(encounterId) }),
    queryClient.invalidateQueries({ queryKey: keys.workflowDeliveries(workflow.workflow_id) }),
  ]);
  const add = async (e) => {
    e.preventDefault();
    if (!choice) return;
    setBusy(true);
    try {
      const { data } = await api.post(`/encounters/${encounterId}/recipients`, { endpoint_id: choice });
      await refresh();
      setChoice('');
      toast.success('Recipient added', {
        description: data.queued?.length ? 'The signed summary is on its way.' : 'They will get the summary once it is signed.',
      });
    } catch (error) {
      toast.error('Could not add the recipient', { description: errorMessage(error) });
    } finally {
      setBusy(false);
    }
  };
  const remove = async (recipientId) => {
    try {
      await api.delete(`/encounters/${encounterId}/recipients/${recipientId}`);
      await refresh();
    } catch (error) {
      toast.error('Could not remove the recipient', { description: errorMessage(error) });
    }
  };

  const taken = new Set(recipients.map(r => r.endpoint_id));
  const available = endpoints.filter(e => !taken.has(e.endpoint_id));
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 space-y-3" data-testid="summary-recipients">
      <div className="flex items-center gap-2">
        <Send className="w-4 h-4 text-zinc-400" />
        <h3 className="text-sm font-medium text-zinc-50">Summary recipients</h3>
        <span className="text-xs text-zinc-500">primary care and receiving facility</span>
      </div>
      {recipients.length === 0 && <p className="text-xs text-zinc-500">No recipients: the signed summary stays in the hospital.</p>}
      <ul className="space-y-2">
        {recipients.map(r => {
          const sent = deliveries.filter(d => d.endpoint_id === r.endpoint_id);
          return (
            <li key={r.recipient_id} className="text-sm text-zinc-300" data-testid="recipient-row">
              <div className="flex items-center gap-2">
                <span>{r.endpoint_name}</span>
                <span className="text-xs text-zinc-500">{r.endpoint_kind.replace('_', ' ')} · {FORMAT_LABELS[r.document_format]}</span>
                {canManage && !readOnly && (
                  <button type="button" aria-label={`Remove ${r.endpoint_name}`} onClick={() => remove(r.recipient_id)}
                          className="ml-auto text-zinc-500 hover:text-zinc-300"><X className="w-4 h-4" /></button>
                )}
              </div>
              {sent.map(d => (
                <div key={d.delivery_id} className="text-xs pl-2">
                  {d.trigger_id.startsWith('add_') ? 'Addendum' : 'Signed summary'}: <DeliveryStatus delivery={d} />
                </div>
              ))}
            </li>
          );
        })}
      </ul>
      {canManage && !readOnly && available.length > 0 && (
        <form onSubmit={add} className="flex gap-2">
          <select aria-label="Recipient" value={choice} onChange={e => setChoice(e.target.value)}
                  className="flex-1 rounded-md bg-zinc-950 border border-zinc-700 px-2 py-1 text-sm text-zinc-200">
            <option value="">Add a recipient…</option>
            {available.map(e => <option key={e.endpoint_id} value={e.endpoint_id}>{e.name}</option>)}
          </select>
          <Button type="submit" size="sm" disabled={!choice || busy} data-testid="add-recipient">
            {busy && <Loader2 className="w-4 h-4 mr-1 animate-spin" />} Add
          </Button>
        </form>
      )}
    </div>
  );
};

const FILTERS = ['failed', 'queued', 'sent', 'acknowledged'];

// The delivery worklist for case managers and admins: failed deliveries first, with retry and cancel. Ids and codes
// only; the link opens the discharge, where the usual access rules apply.
const DeliveriesView = () => {
  const [status, setStatus] = useState('failed');
  const readOnly = useReadOnly();
  const { data: rows = [], isLoading } = useDeliveries(status);
  const [busy, setBusy] = useState(null);

  const act = async (delivery, action) => {
    setBusy(delivery.delivery_id);
    try {
      await api.post(`/deliveries/${delivery.delivery_id}/${action}`);
      await queryClient.invalidateQueries({ queryKey: ['deliveries'] });
      toast.success(action === 'retry' ? 'Delivery retried' : 'Delivery cancelled');
    } catch (error) {
      toast.error(`Could not ${action} the delivery`, { description: errorMessage(error) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="p-6 space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-zinc-50" style={{ fontFamily: 'Outfit, sans-serif' }}>Summary deliveries</h1>
        <p className="text-sm text-zinc-400">Signed discharge summaries sent to primary care and receiving facilities.</p>
      </div>
      <div className="flex gap-2" role="tablist">
        {FILTERS.map(f => (
          <Button key={f} size="sm" variant={f === status ? 'default' : 'outline'} role="tab" aria-selected={f === status}
                  onClick={() => setStatus(f)} className={f === status ? '' : 'border-zinc-700 text-zinc-300'}>
            {STATUS_LABELS[f]}
          </Button>
        ))}
      </div>
      {isLoading ? <Loader2 className="w-5 h-5 animate-spin text-zinc-400" /> : rows.length === 0 ? (
        <p className="text-sm text-zinc-500" data-testid="deliveries-empty">Nothing {STATUS_LABELS[status]}.</p>
      ) : (
        <table className="w-full text-sm text-zinc-300">
          <thead>
            <tr className="text-left text-xs text-zinc-500">
              <th className="py-1">Discharge</th><th>Recipient</th><th>Format</th><th>Status</th><th>Attempts</th><th>Last attempt</th><th />
            </tr>
          </thead>
          <tbody>
            {rows.map(d => (
              <tr key={d.delivery_id} className="border-t border-zinc-800" data-testid="delivery-row">
                <td className="py-2"><Link to={d.link} className="text-blue-400 hover:underline">{d.workflow_id}</Link></td>
                <td>{d.endpoint_name}</td>
                <td>{FORMAT_LABELS[d.document_format]}</td>
                <td><DeliveryStatus delivery={d} /></td>
                <td>{d.attempts}</td>
                <td>{when(d.last_attempt_at)}</td>
                <td className="text-right space-x-2">
                  {d.status === 'failed' && (
                    <Button size="sm" variant="outline" disabled={readOnly || busy === d.delivery_id}
                            onClick={() => act(d, 'retry')} className="border-zinc-700 text-zinc-300">
                      <RotateCcw className="w-3 h-3 mr-1" /> Retry
                    </Button>
                  )}
                  {['failed', 'queued'].includes(d.status) && (
                    <Button size="sm" variant="ghost" disabled={readOnly || busy === d.delivery_id}
                            onClick={() => act(d, 'cancel')} className="text-zinc-400">Cancel</Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};

// The admin's directory of receivers.
export const AdminDeliveryEndpoints = () => {
  const { data: endpoints = [] } = useDeliveryEndpoints(true);
  const readOnly = useReadOnly();
  const [form, setForm] = useState({ name: '', kind: 'primary_care', document_format: 'fhir_bundle', transport: 'https', url: '' });
  const [saving, setSaving] = useState(false);
  const refresh = () => queryClient.invalidateQueries({ queryKey: keys.deliveryEndpoints });

  const create = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      await api.post('/delivery-endpoints', { ...form, url: form.url || null });
      await refresh();
      setForm({ ...form, name: '', url: '' });
    } catch (error) {
      toast.error('Could not add the endpoint', { description: errorMessage(error) });
    } finally {
      setSaving(false);
    }
  };
  const toggle = async (endpoint) => {
    try {
      await api.put(`/delivery-endpoints/${endpoint.endpoint_id}`, { active: !endpoint.active });
      await refresh();
    } catch (error) {
      toast.error('Could not update the endpoint', { description: errorMessage(error) });
    }
  };
  const field = (name) => ({ value: form[name], onChange: e => setForm({ ...form, [name]: e.target.value }) });
  const input = 'rounded-md bg-zinc-950 border border-zinc-700 px-2 py-1 text-sm text-zinc-200';

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 space-y-3" data-testid="delivery-endpoints-card">
      <div className="flex items-center gap-2">
        <Send className="w-4 h-4 text-zinc-400" />
        <h2 className="text-lg font-medium text-zinc-50">Summary delivery endpoints</h2>
      </div>
      <p className="text-xs text-zinc-500">
        Primary care practices and receiving facilities that get signed discharge summaries. A FHIR endpoint is a server
        base URL (the document Bundle is POSTed to /Bundle); a C-CDA endpoint takes the XML at its URL.
      </p>
      {endpoints.length > 0 && (
        <table className="w-full text-xs text-zinc-400">
          <tbody>
            {endpoints.map(e => (
              <tr key={e.endpoint_id} className="border-t border-zinc-800" data-testid="endpoint-row">
                <td className="py-1 text-zinc-200">{e.name}</td>
                <td>{e.kind.replace('_', ' ')}</td>
                <td>{FORMAT_LABELS[e.document_format]}</td>
                <td className="truncate max-w-xs">{e.transport === 'mock' ? 'mock (no network)' : e.url}</td>
                <td className="text-right">
                  <Button size="sm" variant="ghost" disabled={readOnly} onClick={() => toggle(e)} className="text-zinc-400">
                    {e.active ? 'Deactivate' : 'Activate'}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <form onSubmit={create} className="flex flex-wrap gap-2">
        <input aria-label="Endpoint name" placeholder="Name" required className={`${input} flex-1`} {...field('name')} />
        <select aria-label="Endpoint kind" className={input} {...field('kind')}>
          <option value="primary_care">Primary care</option>
          <option value="receiving_facility">Receiving facility</option>
          <option value="other">Other</option>
        </select>
        <select aria-label="Document format" className={input} {...field('document_format')}>
          <option value="fhir_bundle">FHIR document</option>
          <option value="ccda">C-CDA</option>
        </select>
        <select aria-label="Transport" className={input} {...field('transport')}>
          <option value="https">HTTPS</option>
          <option value="mock">Mock (development)</option>
        </select>
        <input aria-label="Endpoint URL" placeholder="https://…" className={`${input} flex-1`} {...field('url')} />
        <Button type="submit" size="sm" disabled={saving || readOnly} data-testid="add-endpoint">Add</Button>
      </form>
    </div>
  );
};

export default DeliveriesView;
