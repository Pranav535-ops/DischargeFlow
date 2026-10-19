import React, { useState } from 'react';
import { Link2, Loader2, Plus } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { keys, queryClient, useReadOnly, useSmartIssuers } from '@/lib/queries';
import { Button } from './ui/button';
import { Input } from './ui/input';

// The EHRs allowed to launch DischargeFlow with SMART on FHIR (ADR-002 item 14). A launch from any other `iss` is
// refused before DischargeFlow contacts it. Register the app in the EHR with the redirect URI {this site}/launch.

const EMPTY = { iss: '', name: '', client_id: '', redirect_uri: '', mrn_system: '', mrn_authority: '', visit_system: '' };

const AdminSmart = () => {
  const { data: issuers = [] } = useSmartIssuers();
  const readOnly = useReadOnly();
  const [form, setForm] = useState({ ...EMPTY, redirect_uri: `${window.location.origin}/launch` });
  const [saving, setSaving] = useState(false);

  const refresh = () => queryClient.invalidateQueries({ queryKey: keys.smartIssuers });
  const clean = (body) => Object.fromEntries(Object.entries(body).map(([k, v]) => [k, typeof v === 'string' && v.trim() === '' && k !== 'iss' ? null : v]));

  const create = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      await api.post('/smart/issuers', clean(form));
      setForm({ ...EMPTY, redirect_uri: `${window.location.origin}/launch` });
      await refresh();
    } catch (error) {
      toast.error('Could not register the EHR', { description: errorMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (issuer) => {
    const body = Object.fromEntries(['iss', 'name', 'client_id', 'redirect_uri', 'mrn_system', 'mrn_authority', 'visit_system']
      .map((k) => [k, issuer[k]]));
    try {
      await api.put(`/smart/issuers/${issuer.issuer_id}`, { ...body, enabled: !issuer.enabled });
      await refresh();
    } catch (error) {
      toast.error('Could not update the EHR', { description: errorMessage(error) });
    }
  };

  const field = (key, label, placeholder, required = false) => (
    <Input aria-label={label} placeholder={placeholder} required={required} value={form[key]}
           onChange={(e) => setForm({ ...form, [key]: e.target.value })}
           className="min-w-[220px] flex-1 bg-zinc-800 border-zinc-700" />
  );

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 space-y-3" data-testid="smart-issuers-card">
      <div className="flex items-center gap-2">
        <Link2 className="w-4 h-4 text-zinc-400" />
        <h2 className="text-lg font-medium text-zinc-50">EHR launch (SMART on FHIR)</h2>
      </div>
      <p className="text-xs text-zinc-500">
        Only these FHIR servers may launch DischargeFlow. The MRN identifier system and assigning authority map the
        EHR's patient to the one the ADT feed admitted; the visit system maps its encounter.
      </p>
      {issuers.length === 0 ? <p className="text-sm text-zinc-500">No EHR registered.</p> : (
        <ul className="divide-y divide-zinc-800">
          {issuers.map((i) => (
            <li key={i.issuer_id} className="py-2 flex items-center justify-between gap-3 text-sm">
              <div className="min-w-0">
                <p className="text-zinc-200">{i.name} <span className="text-zinc-500">· client {i.client_id}</span></p>
                <p className="text-xs text-zinc-500 font-mono truncate">{i.iss}</p>
              </div>
              <Button size="sm" variant="outline" disabled={readOnly} onClick={() => toggle(i)}
                      className={i.enabled ? 'border-zinc-700' : 'border-amber-600 text-amber-400'}>
                {i.enabled ? 'Disable' : 'Enable'}
              </Button>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={create} className="flex flex-wrap gap-2">
        {field('name', 'EHR name', 'Name (e.g. Epic production)', true)}
        {field('iss', 'FHIR base URL (iss)', 'https://ehr.example.org/fhir/R4', true)}
        {field('client_id', 'Client ID', 'Client ID registered in the EHR', true)}
        {field('redirect_uri', 'Redirect URI', 'https://…/launch', true)}
        {field('mrn_system', 'MRN identifier system', 'MRN identifier system (urn:oid:…)')}
        {field('mrn_authority', 'MRN assigning authority', 'Assigning authority (PID-3.4)')}
        {field('visit_system', 'Visit number identifier system', 'Encounter identifier system for PV1-19')}
        <Button type="submit" disabled={saving || readOnly} className="bg-blue-600 hover:bg-blue-500">
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Plus className="w-4 h-4 mr-1" />Register EHR</>}
        </Button>
      </form>
    </div>
  );
};

export default AdminSmart;
