import React, { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { api, errorMessage } from '@/lib/api';
import { invalidateDischarge, useCanBackfill, usePatient, useReadOnly, useWorkflow } from '@/lib/queries';
import { canChangeTaskStatus, isBlocked, taskBackfillBody, taskStatusBody } from '@/lib/tasks';
import { DISPOSITION_LABELS } from '@/lib/plans';
import { 
  ArrowLeft, 
  Clock,
  Check,
  Loader2,
  Sparkles,
  FileText,
  CheckCircle2,
  Circle,
  AlertCircle,
  Pill,
  FileCheck,
  GraduationCap,
  Calendar,
  Receipt,
  Lock
} from 'lucide-react';
import { Button } from '../components/ui/button';
import SummaryLifecycle, { SignByBadge } from './SummaryLifecycle';
import EhrClinicalContext, { EhrSummaryNote } from './EhrClinicalContext';
import { Checkbox } from '../components/ui/checkbox';
import { toast } from 'sonner';


// Route: /workflows/:workflowId
const WorkflowView = () => {
  const { workflowId } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const { data: workflow, isLoading: loading, isError } = useWorkflow(workflowId);
  const { data: patient } = usePatient(workflow?.patient_id);
  // Every change here moves workflow, task, patient and notification state, so refresh all of it.
  const fetchData = invalidateDischarge;
  const onBack = () => navigate(workflow ? `/patients/${workflow.patient_id}` : '/patients');
  const [generatingSummary, setGeneratingSummary] = useState(false);
  const [approvingSummary, setApprovingSummary] = useState(false);
  const [completingWorkflow, setCompletingWorkflow] = useState(false);
  const [updatingTask, setUpdatingTask] = useState(null);
  const [clinicalNotes, setClinicalNotes] = useState('');
  const [editingSummary, setEditingSummary] = useState(false);
  const [editedContent, setEditedContent] = useState('');
  // Downtime (ADR-002 item 6): no writes while read-only; afterwards, paper work is entered with the time it was done.
  const readOnly = useReadOnly();
  const canBackfill = useCanBackfill();
  const [paperTimes, setPaperTimes] = useState({});
  const [paperDischargeTime, setPaperDischargeTime] = useState('');

  const handleGenerateSummary = async () => {
    setGeneratingSummary(true);
    try {
      await api.post(`/ai/generate-summary`, {
        patient_id: workflow.patient_id,
        workflow_id: workflow.workflow_id,
        clinical_notes: clinicalNotes
      });
      setClinicalNotes('');
      toast.success('AI draft generated', { description: 'Review and edit the draft before approving it.' });
      await fetchData();
    } catch (error) {
      toast.error('Failed to generate summary', { description: errorMessage(error) });
    } finally {
      setGeneratingSummary(false);
    }
  };

  const handleApproveSummary = async () => {
    if (!workflow.discharge_summary) return;
    setApprovingSummary(true);
    try {
      const body = editingSummary ? { content: editedContent } : {};
      await api.put(`/ai/approve-summary/${workflow.discharge_summary.summary_id}`, body);
      setEditingSummary(false);
      toast.success('Summary signed', { description: 'Discharge summary approved and signed as version 1.' });
      await fetchData();
    } catch (error) {
      toast.error('Failed to approve summary', { description: errorMessage(error) });
    } finally {
      setApprovingSummary(false);
    }
  };

  const handleTaskUpdate = async (task, newStatus) => {
    const body = taskStatusBody(task, user, newStatus);
    if (!body) return;
    setUpdatingTask(task.task_id);
    try {
      await api.put(`/tasks/${task.task_id}`, body);
      if (newStatus === 'completed') toast.success('Task completed!');
      await fetchData();
    } catch (error) {
      toast.error('Failed to update task', { description: errorMessage(error) });
    } finally {
      setUpdatingTask(null);
    }
  };

  const handleBackfillTask = async (task) => {
    const body = taskBackfillBody(task, user, paperTimes[task.task_id]);
    if (!body) return;
    setUpdatingTask(task.task_id);
    try {
      await api.put(`/tasks/${task.task_id}/backfill`, body);
      toast.success('Paper record entered', { description: `Completed at ${new Date(body.completed_at).toLocaleString()}` });
      setPaperTimes(({ [task.task_id]: _, ...rest }) => rest);
      await fetchData();
    } catch (error) {
      toast.error('Could not enter the paper record', { description: errorMessage(error) });
    } finally {
      setUpdatingTask(null);
    }
  };

  const handleCompleteWorkflow = async () => {
    setCompletingWorkflow(true);
    try {
      const body = paperDischargeTime ? { completed_at: new Date(paperDischargeTime).toISOString() } : {};
      await api.put(`/workflows/${workflow.workflow_id}/complete`, body);
      setPaperDischargeTime('');
      toast.success('Patient discharged!', { description: 'Discharge workflow completed successfully.' });
      await fetchData();
    } catch (error) {
      toast.error('Cannot complete discharge', { description: errorMessage(error) });
    } finally {
      setCompletingWorkflow(false);
    }
  };

  const getCategoryIcon = (category) => {
    const icons = {
      documentation: FileText,
      medication: Pill,
      billing: Receipt,
      follow_up: Calendar,
      education: GraduationCap,
    };
    return icons[category] || FileCheck;
  };

  const getPriorityClass = (priority) => {
    return priority === 1 ? 'priority-high' : priority === 2 ? 'priority-medium' : 'priority-low';
  };

  const canUpdateTask = (task) => canChangeTaskStatus(task, user);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }
  if (isError || !workflow) {
    return (
      <div className="text-center py-16">
        <p className="text-zinc-400 mb-4">Discharge workflow not found.</p>
        <Button variant="outline" onClick={() => navigate('/patients')}>Back to patients</Button>
      </div>
    );
  }

  const canApprove = ['physician', 'admin'].includes(user?.role);
  const allTasksComplete = workflow.tasks?.every(t => t.status === 'completed');
  const summaryApproved = workflow.discharge_summary?.approved;

  return (
    <div className="space-y-6">
      {/* Back button */}
      <Button
        data-testid="back-from-workflow"
        variant="ghost"
        onClick={onBack}
        className="text-zinc-400 hover:text-zinc-200"
      >
        <ArrowLeft className="w-4 h-4 mr-2" />
        Back to Patient
      </Button>

      {/* Workflow Header */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6">
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-semibold text-zinc-50 mb-1" style={{ fontFamily: 'Outfit, sans-serif' }}>
              Discharge Workflow
            </h1>
            <p className="text-zinc-400">{patient?.name} - Room {patient?.room_number}</p>
          </div>
          <span className={`px-3 py-1 rounded text-sm font-medium ${
            workflow.status === 'completed' ? 'status-completed' :
            workflow.status === 'pending_approval' ? 'status-pending' :
            'bg-blue-500/10 text-blue-400 border border-blue-500/20'
          }`}>
            {workflow.status === 'in_progress' ? 'In Progress' :
             workflow.status === 'pending_approval' ? 'Pending Approval' :
             workflow.status === 'externally_discharged' ? 'Patient Left, Tasks Open' :
             workflow.status === 'completed' ? 'Completed' : workflow.status}
          </span>
        </div>

        {workflow.status === 'externally_discharged' && (
          <div data-testid="external-discharge-banner"
               className="mt-4 rounded border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-300">
            The hospital ADT system discharged this patient at {new Date(workflow.departed_at).toLocaleString()}.
            Finish the remaining tasks by {new Date(workflow.documentation_due_at).toLocaleString()}.
          </div>
        )}

        <div className="flex items-center gap-6 mt-4 text-sm text-zinc-400">
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4" />
            Started: {new Date(workflow.started_at).toLocaleString()}
          </div>
          {workflow.plan?.expected_discharge_date ? (
            <div className="flex items-center gap-2" data-testid="workflow-plan">
              <Calendar className="w-4 h-4" />
              Expected discharge: {new Date(`${workflow.plan.expected_discharge_date}T00:00`).toLocaleDateString()}
              {workflow.plan.planned_disposition && ` · ${DISPOSITION_LABELS[workflow.plan.planned_disposition] || workflow.plan.planned_disposition}`}
              {workflow.plan.expected_date_passed && <span className="text-red-400">(date passed)</span>}
            </div>
          ) : workflow.estimated_discharge_time && (
            <div className="flex items-center gap-2">
              <Clock className="w-4 h-4" />
              Est. Discharge: {new Date(workflow.estimated_discharge_time).toLocaleString()}
            </div>
          )}
          <div className="flex items-center gap-2 text-zinc-500">
            {workflow.template_id ? `Template v${workflow.template_version}` : 'Standard checklist'}
          </div>
        </div>

        {/* Progress bar */}
        <div className="mt-6">
          <div className="flex items-center justify-between text-sm text-zinc-400 mb-2">
            <span>Progress</span>
            <span>{workflow.tasks?.filter(t => t.status === 'completed').length} / {workflow.tasks?.length} tasks</span>
          </div>
          <div className="h-2 bg-zinc-800 rounded-full overflow-hidden">
            <div 
              className="h-full bg-blue-600 transition-all duration-300"
              style={{ 
                width: `${(workflow.tasks?.filter(t => t.status === 'completed').length / workflow.tasks?.length) * 100}%` 
              }}
            />
          </div>
        </div>

        {/* Complete workflow button */}
        {canApprove && allTasksComplete && summaryApproved && workflow.status !== 'completed' && (
          <div className="mt-6 flex flex-wrap items-center gap-3">
          <Button
            data-testid="complete-workflow-btn"
            onClick={handleCompleteWorkflow}
            disabled={completingWorkflow || readOnly}
            className="bg-emerald-600 hover:bg-emerald-500"
          >
            {completingWorkflow ? (
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
            ) : (
              <CheckCircle2 className="w-4 h-4 mr-2" />
            )}
            {paperDischargeTime ? 'Enter Paper Discharge' : 'Complete Discharge'}
          </Button>
          {canBackfill && workflow.status !== 'externally_discharged' && (
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              Discharged on paper during a downtime at
              <input
                type="datetime-local"
                data-testid="paper-discharge-time"
                value={paperDischargeTime}
                onChange={e => setPaperDischargeTime(e.target.value)}
                className="h-8 rounded bg-zinc-950 border border-zinc-700 px-2 text-zinc-200"
              />
            </label>
          )}
          </div>
        )}
      </div>

      {/* EHR medications, problems and allergies when launched from the EHR (ADR-002 item 15) */}
      <EhrClinicalContext patientId={workflow.patient_id} />

      {/* AI Discharge Summary */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <Sparkles className="w-5 h-5 text-blue-400" strokeWidth={1.5} />
            <h2 className="text-lg font-medium text-zinc-50">AI Discharge Summary</h2>
          </div>
          {workflow.discharge_summary?.approved ? (
            <span className="flex items-center gap-1 text-emerald-400 text-sm" data-testid="summary-signed">
              <CheckCircle2 className="w-4 h-4" />
              Signed
            </span>
          ) : (
            <SignByBadge workflow={workflow} />
          )}
        </div>

        {workflow.discharge_summary ? (
          <div>
            {(() => {
              const summary = workflow.discharge_summary;
              const gen = summary.generation;
              const warnings = gen?.grounding_warnings || [];
              return (
                <>
                  {gen && (
                    <p className="text-xs text-zinc-500 mb-3" data-testid="summary-provenance">
                      {summary.approved ? 'Generated by' : 'Draft generated by'} {gen.model_id} ({gen.provider}) · prompt {gen.prompt_version}
                      {summary.generated_by === 'mock' && ' · offline mock, not a real model'}
                    </p>
                  )}
                  {!summary.approved && warnings.length > 0 && (
                    <div className="mb-4 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-300" data-testid="grounding-warnings">
                      <p className="flex items-center gap-2 font-medium mb-1">
                        <AlertCircle className="w-4 h-4" /> Check before approving: not found in the source record
                      </p>
                      <ul className="list-disc pl-6">
                        {warnings.map((w, i) => <li key={i}>{w.type}: {w.value}</li>)}
                      </ul>
                    </div>
                  )}
                  {editingSummary ? (
                    <textarea
                      data-testid="summary-editor"
                      value={editedContent}
                      onChange={e => setEditedContent(e.target.value)}
                      rows={18}
                      className="w-full rounded-md bg-zinc-950 border border-zinc-700 p-3 text-sm text-zinc-200 font-sans leading-relaxed focus:outline-none focus:border-blue-500"
                    />
                  ) : (
                    <div className="ai-summary-content">
                      <pre className="whitespace-pre-wrap text-sm text-zinc-300 font-sans leading-relaxed">
                        {summary.content}
                      </pre>
                    </div>
                  )}
                  {summary.approved && summary.edit_similarity != null && summary.edit_similarity < 1 && (
                    <p className="text-xs text-zinc-500 mt-3">
                      Approved with physician edits ({Math.round(summary.edit_similarity * 100)}% similar to the AI draft)
                    </p>
                  )}
                  {summary.approved && (
                    <SummaryLifecycle summary={summary} canSign={canApprove} readOnly={readOnly} onChanged={fetchData} />
                  )}
                  {!summary.approved && canApprove && (
                    <div className="mt-4 flex gap-3">
                      <Button
                        data-testid="approve-summary-btn"
                        onClick={handleApproveSummary}
                        disabled={approvingSummary || readOnly}
                        className="bg-emerald-600 hover:bg-emerald-500"
                      >
                        {approvingSummary ? (
                          <Loader2 className="w-4 h-4 animate-spin mr-2" />
                        ) : (
                          <Check className="w-4 h-4 mr-2" />
                        )}
                        {editingSummary ? 'Approve and Sign Edited Summary' : 'Approve and Sign'}
                      </Button>
                      <Button
                        data-testid="edit-summary-btn"
                        variant="outline"
                        disabled={readOnly}
                        onClick={() => {
                          if (!editingSummary) setEditedContent(summary.content);
                          setEditingSummary(!editingSummary);
                        }}
                        className="border-zinc-700 text-zinc-300"
                      >
                        <FileText className="w-4 h-4 mr-2" />
                        {editingSummary ? 'Discard Edits' : 'Edit Draft'}
                      </Button>
                    </div>
                  )}
                </>
              );
            })()}
          </div>
        ) : (
          <div className="text-center py-8">
            <Sparkles className="w-10 h-10 text-zinc-700 mx-auto mb-3" />
            <p className="text-zinc-400 mb-4">No discharge summary generated yet</p>
            {canApprove && <div className="flex justify-center mb-2"><EhrSummaryNote patientId={workflow.patient_id} /></div>}
            {canApprove && (
              <textarea
                data-testid="clinical-notes-input"
                value={clinicalNotes}
                onChange={e => setClinicalNotes(e.target.value)}
                rows={5}
                maxLength={20000}
                placeholder={'Optional clinical notes for the AI draft, e.g.\nCourse: ...\nMedications: ...\nFollow-up: ...\nIdentifiers are removed before the AI sees them, and it may only use facts given here.'}
                className="w-full max-w-2xl mx-auto mb-4 block rounded-md bg-zinc-950 border border-zinc-700 p-3 text-sm text-zinc-200 text-left focus:outline-none focus:border-blue-500 placeholder-zinc-600"
              />
            )}
            {canApprove && (
              <Button
                data-testid="generate-summary-btn"
                onClick={handleGenerateSummary}
                disabled={generatingSummary || readOnly}
                className="bg-blue-600 hover:bg-blue-500"
              >
                {generatingSummary ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin mr-2" />
                    Generating...
                  </>
                ) : (
                  <>
                    <Sparkles className="w-4 h-4 mr-2" />
                    Generate AI Summary
                  </>
                )}
              </Button>
            )}
          </div>
        )}
      </div>

      {/* Tasks */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6">
        <h2 className="text-lg font-medium text-zinc-50 mb-4">Discharge Tasks</h2>
        
        <div className="space-y-3">
          {workflow.tasks?.map((task) => {
            const Icon = getCategoryIcon(task.category);
            const isUpdating = updatingTask === task.task_id;
            const canEdit = canUpdateTask(task);
            const blocked = task.status !== 'completed' && isBlocked(task);
            
            return (
              <div
                key={task.task_id}
                data-testid={`task-${task.task_id}`}
                className={`p-4 rounded-lg bg-zinc-800/50 border border-zinc-800 ${getPriorityClass(task.priority)}`}
              >
                <div className="flex items-start gap-3">
                  {canEdit ? (
                    <Checkbox
                      data-testid={`task-checkbox-${task.task_id}`}
                      checked={task.status === 'completed'}
                      disabled={isUpdating || readOnly || task.status === 'completed' || blocked}
                      onCheckedChange={(checked) => {
                        handleTaskUpdate(task, checked ? 'completed' : 'pending');
                      }}
                      className="mt-1"
                    />
                  ) : (
                    <div className="mt-1">
                      {task.status === 'completed' ? (
                        <CheckCircle2 className="w-5 h-5 text-emerald-400" />
                      ) : task.status === 'in_progress' ? (
                        <AlertCircle className="w-5 h-5 text-amber-400" />
                      ) : (
                        <Circle className="w-5 h-5 text-zinc-600" />
                      )}
                    </div>
                  )}
                  
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <Icon className="w-4 h-4 text-zinc-500" strokeWidth={1.5} />
                      <span className={`font-medium ${task.status === 'completed' ? 'text-zinc-500 line-through' : 'text-zinc-200'}`}>
                        {task.title}
                      </span>
                    </div>
                    <p className="text-sm text-zinc-500 mt-1">{task.description}</p>
                    <div className="flex items-center gap-4 mt-2 text-xs">
                      <span className={`px-2 py-0.5 rounded capitalize ${
                        task.assigned_role === 'physician' ? 'bg-blue-500/10 text-blue-400' :
                        task.assigned_role === 'nurse' ? 'bg-emerald-500/10 text-emerald-400' :
                        'bg-amber-500/10 text-amber-400'
                      }`}>
                        {task.assigned_role}
                      </span>
                      <span className="text-zinc-600">
                        Priority: {task.priority === 1 ? 'High' : task.priority === 2 ? 'Medium' : 'Low'}
                      </span>
                      {task.completed_at ? (
                        <span className="text-zinc-600">
                          Completed: {new Date(task.completed_at).toLocaleString()}
                          {task.backfilled_at && ' (on paper, entered after a downtime)'}
                        </span>
                      ) : task.due && (
                        <span className={task.overdue ? 'text-red-400' : 'text-zinc-600'} data-testid={`task-due-${task.task_id}`}>
                          {task.overdue ? 'Overdue since' : 'Due'} {new Date(task.due).toLocaleString()}
                          {task.overdue && task.delay_recorded && ' (delay reason recorded)'}
                        </span>
                      )}
                    </div>
                    {blocked && (
                      <p className="flex items-center gap-1 text-xs text-amber-400 mt-2" data-testid={`task-blocked-${task.task_id}`}>
                        <Lock className="w-3 h-3" />
                        Waiting on: {task.blocked_by.map(b => b.title).join(', ')}
                      </p>
                    )}
                    {canBackfill && canEdit && task.status !== 'completed' && (
                      <div className="flex flex-wrap items-center gap-2 mt-2 text-xs text-zinc-400">
                        Done on paper at
                        <input
                          type="datetime-local"
                          aria-label={`Time "${task.title}" was done`}
                          data-testid={`paper-time-${task.task_id}`}
                          value={paperTimes[task.task_id] || ''}
                          onChange={e => setPaperTimes({ ...paperTimes, [task.task_id]: e.target.value })}
                          className="h-7 rounded bg-zinc-950 border border-zinc-700 px-2 text-zinc-200"
                        />
                        <Button
                          size="sm"
                          variant="outline"
                          data-testid={`paper-enter-${task.task_id}`}
                          disabled={!paperTimes[task.task_id] || isUpdating}
                          onClick={() => handleBackfillTask(task)}
                          className="h-7 border-zinc-700 text-zinc-300"
                        >
                          Enter
                        </Button>
                      </div>
                    )}
                  </div>

                  {isUpdating && (
                    <Loader2 className="w-4 h-4 animate-spin text-blue-500" />
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};

export default WorkflowView;
