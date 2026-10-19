import React, { useState } from 'react';
import { Building2, ChevronDown, Loader2, Plus, ShieldAlert } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { keys, queryClient, useAccessGrants, useUnits } from '@/lib/queries';
import { Button } from './ui/button';
import { Input } from './ui/input';
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuTrigger,
} from './ui/dropdown-menu';

// Units and break-glass review for the admin screen (ADR-002 item 3). With UNIT_SCOPED_ACCESS on, staff see the
// patients on their units, unassigned patients, and those they opened with break-glass.

export const UnitsCard = () => {
  const { data: units = [] } = useUnits();
  const [form, setForm] = useState({ code: '', name: '' });
  const [saving, setSaving] = useState(false);

  const create = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      await api.post('/units', form);
      setForm({ code: '', name: '' });
      await queryClient.invalidateQueries({ queryKey: keys.units });
    } catch (error) {
      toast.error('Could not add the unit', { description: errorMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 space-y-3" data-testid="units-card">
      <div className="flex items-center gap-2">
        <Building2 className="w-4 h-4 text-zinc-400" />
        <h2 className="text-lg font-medium text-zinc-50">Units</h2>
      </div>
      <p className="text-xs text-zinc-500">
        Use the code your ADT feed sends as the point of care (PV1-3), so admissions land on the right unit.
      </p>
      <div className="flex flex-wrap gap-2">
        {units.length === 0 && <span className="text-sm text-zinc-500">No units yet.</span>}
        {units.map(u => (
          <span key={u.unit_id} className="px-2 py-1 rounded bg-zinc-800 text-xs text-zinc-300">
            <span className="font-mono">{u.code}</span> · {u.name}
          </span>
        ))}
      </div>
      <form onSubmit={create} className="flex flex-wrap gap-2">
        <Input aria-label="Unit code" placeholder="Code (e.g. 4W)" required value={form.code} maxLength={40}
               onChange={(e) => setForm({ ...form, code: e.target.value })} className="w-36 bg-zinc-800 border-zinc-700" />
        <Input aria-label="Unit name" placeholder="Name (e.g. 4 West Medicine)" required value={form.name}
               onChange={(e) => setForm({ ...form, name: e.target.value })} className="flex-1 min-w-[180px] bg-zinc-800 border-zinc-700" />
        <Button type="submit" disabled={saving} className="bg-blue-600 hover:bg-blue-500">
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <><Plus className="w-4 h-4 mr-1" />Add unit</>}
        </Button>
      </form>
    </div>
  );
};

// Each tick saves at once: PUT /users/:id/units replaces the whole set.
export const UserUnitsPicker = ({ user }) => {
  const { data: units = [] } = useUnits();
  const [saving, setSaving] = useState(false);
  const current = user.unit_ids || [];
  const label = units.filter(u => current.includes(u.unit_id)).map(u => u.code).join(', ') || 'No units';

  const toggle = async (unitId, on) => {
    const next = on ? [...current, unitId] : current.filter(id => id !== unitId);
    setSaving(true);
    try {
      await api.put(`/users/${user.user_id}/units`, { unit_ids: next });
      await queryClient.invalidateQueries({ queryKey: keys.users });
    } catch (error) {
      toast.error('Could not update units', { description: errorMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  if (units.length === 0) return <span className="text-xs text-zinc-600">-</span>;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" data-testid={`units-${user.user_id}`} disabled={saving}
                className="bg-zinc-800 border-zinc-700 text-zinc-300">
          {label} <ChevronDown className="w-3 h-3 ml-1" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent className="bg-zinc-800 border-zinc-700">
        {units.map(u => (
          <DropdownMenuCheckboxItem key={u.unit_id} checked={current.includes(u.unit_id)}
                                    onCheckedChange={(on) => toggle(u.unit_id, on)}>
            {u.code} · {u.name}
          </DropdownMenuCheckboxItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
};

export const BreakGlassReview = () => {
  const { data: grants = [], isLoading } = useAccessGrants();
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg overflow-hidden" data-testid="break-glass-review">
      <div className="p-4 border-b border-zinc-800 flex items-center gap-2">
        <ShieldAlert className="w-4 h-4 text-amber-400" />
        <h2 className="text-lg font-medium text-zinc-50">Break-glass access</h2>
        <span className="text-xs text-zinc-500">Patients opened outside the user&apos;s units, for review</span>
      </div>
      {isLoading ? null : grants.length === 0 ? (
        <p className="p-4 text-sm text-zinc-500">No emergency access so far.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-zinc-800/50 text-left text-xs text-zinc-500 uppercase">
              <th className="p-3">When</th><th className="p-3">User</th><th className="p-3">Patient</th>
              <th className="p-3">Reason</th><th className="p-3">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {grants.map(g => (
              <tr key={g.grant_id}>
                <td className="p-3 text-zinc-400 whitespace-nowrap">{new Date(g.created_at).toLocaleString()}</td>
                <td className="p-3 text-zinc-200">{g.user_name}</td>
                <td className="p-3 font-mono text-xs text-zinc-400">{g.patient_id}</td>
                <td className="p-3 text-zinc-300">{g.reason}</td>
                <td className="p-3 text-xs">{g.active ? <span className="text-amber-400">Active</span> : <span className="text-zinc-500">Expired</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};
