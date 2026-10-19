import React, { useState } from 'react';
import { ListChecks, Plus, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { keys, queryClient, useTemplates } from '@/lib/queries';
import { DISPOSITION_LABELS } from '@/lib/plans';
import { Button } from './ui/button';
import { Input } from './ui/input';

const ROLES = ['physician', 'nurse', 'pharmacist', 'admin'];
const blankTask = (n) => ({
  key: `task_${n}`, title: '', description: '', category: 'general', assigned_role: 'nurse', priority: 2,
  due_offset_hours: null, depends_on: [],
});

// The editable copy of a template: prerequisites as a comma-separated list of keys, the due offset as text.
const toForm = (template) => ({
  ...template,
  tasks: template.tasks.map(t => ({ ...t, depends_on: (t.depends_on || []).join(', '),
    due_offset_hours: t.due_offset_hours ?? '' })),
});
const fromForm = (form) => ({
  disposition: form.disposition,
  name: form.name,
  active: form.active,
  tasks: form.tasks.map(t => ({
    key: t.key.trim(), title: t.title.trim(), description: t.description || '', category: t.category || 'general',
    assigned_role: t.assigned_role, priority: Number(t.priority),
    due_offset_hours: t.due_offset_hours === '' ? null : Number(t.due_offset_hours),
    depends_on: String(t.depends_on || '').split(',').map(s => s.trim()).filter(Boolean),
  })),
});

// Workflow templates per disposition (ADR-002 item 9). Each task has a key, a role, a due time in hours from 00:00
// on the expected discharge date (negative: the days before), and prerequisites by key. Saving bumps the version;
// discharges already started keep their tasks.
const AdminTemplates = () => {
  const { data: templates = [] } = useTemplates();
  const [form, setForm] = useState(null);
  const [saving, setSaving] = useState(false);
  const missing = Object.keys(DISPOSITION_LABELS).filter(d => !templates.some(t => t.disposition === d));

  const setTask = (i, field, value) =>
    setForm({ ...form, tasks: form.tasks.map((t, j) => (j === i ? { ...t, [field]: value } : t)) });

  const save = async () => {
    setSaving(true);
    try {
      const body = fromForm(form);
      if (form.template_id) await api.put(`/workflow-templates/${form.template_id}`, body);
      else await api.post('/workflow-templates', body);
      toast.success('Template saved');
      setForm(null);
      await queryClient.invalidateQueries({ queryKey: keys.templates });
    } catch (error) {
      const detail = error?.response?.data?.detail;
      const description = Array.isArray(detail) ? detail.map(d => d.msg).join('; ') : errorMessage(error);
      toast.error('Could not save the template', { description });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 space-y-3" data-testid="templates-card">
      <div className="flex items-center gap-2">
        <ListChecks className="w-4 h-4 text-zinc-400" />
        <h2 className="text-lg font-medium text-zinc-50">Workflow templates</h2>
      </div>
      <p className="text-xs text-zinc-500">
        The planned disposition picks the template when a discharge starts; without a plan or an active template the
        standard checklist is used.
      </p>
      <ul className="divide-y divide-zinc-800 text-sm">
        {templates.map(t => (
          <li key={t.template_id} className="flex items-center justify-between py-2">
            <span className="text-zinc-200">
              {t.name} <span className="text-zinc-500">({DISPOSITION_LABELS[t.disposition] || t.disposition},
              v{t.version}, {t.tasks.length} tasks{t.active ? '' : ', inactive'})</span>
            </span>
            <Button size="sm" variant="outline" className="border-zinc-700 text-zinc-300"
              onClick={() => setForm(toForm(t))} data-testid={`edit-template-${t.disposition}`}>Edit</Button>
          </li>
        ))}
      </ul>
      {!form && missing.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {missing.map(d => (
            <Button key={d} size="sm" variant="outline" className="border-zinc-700 text-zinc-300"
              onClick={() => setForm({ disposition: d, name: DISPOSITION_LABELS[d], active: true,
                tasks: [toForm({ tasks: [blankTask(1)] }).tasks[0]] })}>
              <Plus className="w-3 h-3 mr-1" /> {DISPOSITION_LABELS[d]}
            </Button>
          ))}
        </div>
      )}

      {form && (
        <div className="space-y-3 border-t border-zinc-800 pt-3" data-testid="template-form">
          <div className="flex flex-wrap items-center gap-3">
            <Input aria-label="Template name" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
              className="max-w-xs bg-zinc-950 border-zinc-700" />
            <label className="flex items-center gap-2 text-sm text-zinc-400">
              <input type="checkbox" checked={form.active} onChange={e => setForm({ ...form, active: e.target.checked })} />
              Active
            </label>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead className="text-left text-zinc-500">
                <tr>
                  <th className="font-normal">Key</th><th className="font-normal">Title</th>
                  <th className="font-normal">Role</th><th className="font-normal">Priority</th>
                  <th className="font-normal">Due (h from 00:00)</th><th className="font-normal">After (keys)</th><th />
                </tr>
              </thead>
              <tbody>
                {form.tasks.map((t, i) => (
                  <tr key={i}>
                    <td><Input aria-label={`Key ${i + 1}`} value={t.key} onChange={e => setTask(i, 'key', e.target.value)}
                      className="h-8 w-28 bg-zinc-950 border-zinc-700" /></td>
                    <td><Input aria-label={`Title ${i + 1}`} value={t.title} onChange={e => setTask(i, 'title', e.target.value)}
                      className="h-8 min-w-48 bg-zinc-950 border-zinc-700" /></td>
                    <td>
                      <select aria-label={`Role ${i + 1}`} value={t.assigned_role} onChange={e => setTask(i, 'assigned_role', e.target.value)}
                        className="h-8 rounded bg-zinc-950 border border-zinc-700 px-1 text-zinc-200">
                        {ROLES.map(r => <option key={r} value={r}>{r}</option>)}
                      </select>
                    </td>
                    <td>
                      <select aria-label={`Priority ${i + 1}`} value={t.priority} onChange={e => setTask(i, 'priority', e.target.value)}
                        className="h-8 rounded bg-zinc-950 border border-zinc-700 px-1 text-zinc-200">
                        <option value={1}>High</option><option value={2}>Medium</option><option value={3}>Low</option>
                      </select>
                    </td>
                    <td><Input aria-label={`Due offset ${i + 1}`} type="number" value={t.due_offset_hours}
                      onChange={e => setTask(i, 'due_offset_hours', e.target.value)} className="h-8 w-24 bg-zinc-950 border-zinc-700" /></td>
                    <td><Input aria-label={`Prerequisites ${i + 1}`} value={t.depends_on}
                      onChange={e => setTask(i, 'depends_on', e.target.value)} className="h-8 w-36 bg-zinc-950 border-zinc-700" /></td>
                    <td>
                      <Button size="sm" variant="ghost" aria-label={`Remove task ${i + 1}`}
                        onClick={() => setForm({ ...form, tasks: form.tasks.filter((_, j) => j !== i) })}>
                        <Trash2 className="w-3 h-3" />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" className="border-zinc-700 text-zinc-300"
              onClick={() => setForm({ ...form, tasks: [...form.tasks, toForm({ tasks: [blankTask(form.tasks.length + 1)] }).tasks[0]] })}>
              <Plus className="w-3 h-3 mr-1" /> Task
            </Button>
            <Button size="sm" onClick={save} disabled={saving} data-testid="save-template">Save</Button>
            <Button size="sm" variant="ghost" onClick={() => setForm(null)}>Cancel</Button>
          </div>
        </div>
      )}
    </div>
  );
};

export default AdminTemplates;
