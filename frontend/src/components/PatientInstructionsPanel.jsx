import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, CheckCircle2, FileText, Loader2, PenLine, Printer, Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { LANGUAGES, languageName } from '@/lib/languages';
import { keys, queryClient, usePatientInstructions } from '@/lib/queries';
import { Button } from './ui/button';

// Who drafts, edits and approves patient instructions (instructions:manage). The API enforces it.
export const canManageInstructions = (role) => ['physician', 'nurse', 'admin'].includes(role);

const WARNING_LABELS = {
  medication: 'Medicine not in the signed summary',
  diagnosis: 'Condition not in the signed summary',
  omission: 'Discharge medicine missing',
  readability: 'Hard to read',
};

const Grade = ({ item }) => {
  if (!item.english_readability) {
    return <span className="text-zinc-500">Reading grade not measured for {item.language_name}</span>;
  }
  const grade = item.approved ? item.approved_reading_grade ?? item.reading_grade : item.reading_grade;
  if (grade == null) return null;
  return (
    <span data-testid="reading-grade" className={grade > item.target_grade ? 'text-amber-400' : 'text-emerald-400'}>
      Reading grade {grade} (target {item.target_grade} or below)
    </span>
  );
};

const InstructionsCard = ({ item, canManage, readOnly, onChanged }) => {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(item.content);
  const [saving, setSaving] = useState(false);
  const gen = item.generation;
  const warnings = gen?.grounding_warnings || [];

  const approve = async () => {
    setSaving(true);
    try {
      await api.put(`/ai/patient-instructions/${item.instructions_id}/approve`, editing ? { content: text } : {});
      await onChanged();
      setEditing(false);
      toast.success('Instructions approved', { description: 'They can now be printed for the patient.' });
    } catch (error) {
      toast.error('Failed to approve instructions', { description: errorMessage(error) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-md border border-zinc-800 bg-zinc-950 p-4 space-y-3" data-testid="instructions-card">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-zinc-200">{item.language_name}</span>
          {item.approved ? (
            <span className="flex items-center gap-1 text-emerald-400 text-xs" data-testid="instructions-approved">
              <CheckCircle2 className="w-3.5 h-3.5" /> Approved
            </span>
          ) : (
            <span className="text-xs text-amber-400">Draft, not yet approved</span>
          )}
        </div>
        <p className="text-xs"><Grade item={item} /></p>
      </div>

      {gen && (
        <p className="text-xs text-zinc-500" data-testid="instructions-provenance">
          {item.approved ? 'Drafted by' : 'Draft by'} {gen.model_id} ({gen.provider}) · prompt {gen.prompt_version}
          {item.generated_by === 'mock' && ' · offline mock, not a real model (it does not translate)'}
        </p>
      )}
      {item.stale && (
        <p className="flex items-center gap-2 text-xs text-amber-400" data-testid="instructions-stale">
          <AlertTriangle className="w-4 h-4" />
          An addendum was signed after these were drafted. Check them against it.
        </p>
      )}
      {!item.approved && warnings.length > 0 && (
        <ul className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200 space-y-1"
            data-testid="instructions-warnings">
          {warnings.map((w, i) => (
            <li key={i}><span className="font-medium">{WARNING_LABELS[w.type] || w.type}:</span> {w.value} ({w.reason})</li>
          ))}
        </ul>
      )}

      {editing ? (
        <textarea
          data-testid="instructions-editor"
          aria-label="Edit patient instructions"
          value={text}
          onChange={e => setText(e.target.value)}
          rows={16}
          maxLength={50000}
          className="w-full rounded-md bg-zinc-900 border border-zinc-700 p-3 text-sm text-zinc-200 focus:outline-none focus:border-blue-500"
        />
      ) : (
        <pre className="whitespace-pre-wrap text-sm text-zinc-300 font-sans leading-relaxed"
             data-testid="instructions-content">{item.content}</pre>
      )}

      {item.approved && item.edit_similarity != null && item.edit_similarity < 1 && (
        <p className="text-xs text-zinc-500">
          Approved with edits ({Math.round(item.edit_similarity * 100)}% similar to the draft)
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        {!item.approved && canManage && (
          <>
            <Button data-testid="approve-instructions-btn" onClick={approve} disabled={saving || readOnly}
                    className="bg-emerald-600 hover:bg-emerald-500">
              {saving ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <CheckCircle2 className="w-4 h-4 mr-2" />}
              {editing ? 'Approve Edited Instructions' : 'Approve'}
            </Button>
            <Button variant="outline" data-testid="edit-instructions-btn" disabled={readOnly}
                    onClick={() => { setText(item.content); setEditing(!editing); }}>
              <PenLine className="w-4 h-4 mr-2" />
              {editing ? 'Cancel Edit' : 'Edit'}
            </Button>
          </>
        )}
        {item.approved && canManage && (
          <Button asChild variant="outline" data-testid="print-instructions-btn">
            <Link to={`/instructions/${item.instructions_id}/print`}>
              <Printer className="w-4 h-4 mr-2" /> Print for the patient
            </Link>
          </Button>
        )}
      </div>
    </div>
  );
};

// Patient-facing discharge instructions (ADR-002 item 18), drafted from the signed summary in the patient's
// language. A nurse or physician reviews, edits and approves each draft; only approved instructions print.
const PatientInstructionsPanel = ({ workflow, patient, role, readOnly }) => {
  const summarySigned = !!workflow.discharge_summary?.approved;
  const { data: items = [], isLoading } = usePatientInstructions(workflow.workflow_id, summarySigned);
  const canManage = canManageInstructions(role);
  const [language, setLanguage] = useState('');
  const [generating, setGenerating] = useState(false);
  const chosen = language || patient?.preferred_language || 'en';
  const refresh = () => queryClient.invalidateQueries({ queryKey: keys.patientInstructions(workflow.workflow_id) });

  const generate = async () => {
    setGenerating(true);
    try {
      await api.post('/ai/patient-instructions', { workflow_id: workflow.workflow_id, language: chosen });
      await refresh();
      toast.success('Instructions drafted', { description: 'Review and approve them before printing.' });
    } catch (error) {
      toast.error('Failed to draft instructions', { description: errorMessage(error) });
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6" data-testid="patient-instructions">
      <div className="flex items-center gap-3 mb-2">
        <FileText className="w-5 h-5 text-blue-400" strokeWidth={1.5} />
        <h2 className="text-lg font-medium text-zinc-50">Patient Instructions</h2>
      </div>
      <p className="text-sm text-zinc-400 mb-4">
        Plain-language instructions for the patient, drafted from the signed summary in their preferred language
        ({languageName(patient?.preferred_language)}). Every draft needs a nurse or physician to review and approve it.
      </p>

      {!summarySigned ? (
        <p className="text-sm text-zinc-500" data-testid="instructions-need-signature">
          Available once the discharge summary is signed.
        </p>
      ) : (
        <div className="space-y-4">
          {isLoading && <Loader2 className="w-5 h-5 animate-spin text-blue-500" />}
          {items.map(item => (
            <InstructionsCard key={item.instructions_id} item={item} canManage={canManage} readOnly={readOnly}
                              onChanged={refresh} />
          ))}
          {canManage && (
            <div className="flex flex-wrap items-center gap-2">
              <select
                data-testid="instructions-language"
                aria-label="Language for the instructions"
                value={chosen}
                onChange={e => setLanguage(e.target.value)}
                className="h-9 rounded-md bg-zinc-950 border border-zinc-700 px-3 text-sm text-zinc-200"
              >
                {!LANGUAGES.some(([c]) => c === chosen) && <option value={chosen}>{languageName(chosen)}</option>}
                {LANGUAGES.map(([code, name]) => <option key={code} value={code}>{name}</option>)}
              </select>
              <Button data-testid="generate-instructions-btn" onClick={generate} disabled={generating || readOnly}
                      className="bg-blue-600 hover:bg-blue-500">
                {generating ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Sparkles className="w-4 h-4 mr-2" />}
                {items.some(i => i.language === chosen && !i.approved) ? 'Redraft' : 'Draft Instructions'}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

export default PatientInstructionsPanel;
