import React, { useState, useEffect } from 'react';
import { useAuth } from '../context/AuthContext';
import axios from 'axios';
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
  Receipt
} from 'lucide-react';
import { Button } from '../components/ui/button';
import { Checkbox } from '../components/ui/checkbox';
import { toast } from 'sonner';

const API = `${process.env.REACT_APP_BACKEND_URL || ""}/api`;

const WorkflowView = ({ workflow: initialWorkflow, onBack, onRefreshNotifications }) => {
  const { user } = useAuth();
  const [workflow, setWorkflow] = useState(initialWorkflow);
  const [patient, setPatient] = useState(null);
  const [loading, setLoading] = useState(true);
  const [generatingSummary, setGeneratingSummary] = useState(false);
  const [approvingSummary, setApprovingSummary] = useState(false);
  const [completingWorkflow, setCompletingWorkflow] = useState(false);
  const [updatingTask, setUpdatingTask] = useState(null);
  const [clinicalNotes, setClinicalNotes] = useState('');
  const [editingSummary, setEditingSummary] = useState(false);
  const [editedContent, setEditedContent] = useState('');

  const fetchData = async () => {
    try {
      const [wfRes, patRes] = await Promise.all([
        axios.get(`${API}/workflows/${workflow.workflow_id}`, { withCredentials: true }),
        axios.get(`${API}/patients/${workflow.patient_id}`, { withCredentials: true })
      ]);
      setWorkflow(wfRes.data);
      setPatient(patRes.data);
    } catch (error) {
      console.error('Fetch data error:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflow.workflow_id]);

  const handleGenerateSummary = async () => {
    setGeneratingSummary(true);
    try {
      await axios.post(`${API}/ai/generate-summary`, {
        patient_id: workflow.patient_id,
        workflow_id: workflow.workflow_id,
        clinical_notes: clinicalNotes
      }, { withCredentials: true });
      setClinicalNotes('');
      toast.success('AI draft generated', { description: 'Review and edit the draft before approving it.' });
      fetchData();
    } catch (error) {
      console.error('Generate summary error:', error);
      toast.error('Failed to generate summary', { description: error.response?.data?.detail || 'An error occurred.' });
    } finally {
      setGeneratingSummary(false);
    }
  };

  const handleApproveSummary = async () => {
    if (!workflow.discharge_summary) return;
    setApprovingSummary(true);
    try {
      const body = editingSummary ? { content: editedContent } : {};
      await axios.put(`${API}/ai/approve-summary/${workflow.discharge_summary.summary_id}`, body, { withCredentials: true });
      setEditingSummary(false);
      toast.success('Summary approved', { description: 'Discharge summary has been approved.' });
      fetchData();
      onRefreshNotifications();
    } catch (error) {
      console.error('Approve summary error:', error);
      toast.error('Failed to approve summary');
    } finally {
      setApprovingSummary(false);
    }
  };

  const handleTaskUpdate = async (taskId, newStatus) => {
    setUpdatingTask(taskId);
    try {
      await axios.put(`${API}/tasks/${taskId}`, { status: newStatus }, { withCredentials: true });
      if (newStatus === 'completed') toast.success('Task completed!');
      fetchData();
      onRefreshNotifications();
    } catch (error) {
      console.error('Update task error:', error);
      toast.error('Failed to update task');
    } finally {
      setUpdatingTask(null);
    }
  };

  const handleCompleteWorkflow = async () => {
    setCompletingWorkflow(true);
    try {
      await axios.put(`${API}/workflows/${workflow.workflow_id}/complete`, {}, { withCredentials: true });
      toast.success('Patient discharged!', { description: 'Discharge workflow completed successfully.' });
      fetchData();
      onRefreshNotifications();
    } catch (error) {
      console.error('Complete workflow error:', error);
      toast.error('Cannot complete discharge', { description: error.response?.data?.detail || 'An error occurred.' });
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

  const canUpdateTask = (task) => {
    return task.assigned_role === user?.role || user?.role === 'admin';
  };

  const canApprove = ['physician', 'admin'].includes(user?.role);
  const allTasksComplete = workflow.tasks?.every(t => t.status === 'completed');
  const summaryApproved = workflow.discharge_summary?.approved;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }

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
        Back to Patients
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
             workflow.status === 'completed' ? 'Completed' : workflow.status}
          </span>
        </div>

        <div className="flex items-center gap-6 mt-4 text-sm text-zinc-400">
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4" />
            Started: {new Date(workflow.started_at).toLocaleString()}
          </div>
          {workflow.estimated_discharge_time && (
            <div className="flex items-center gap-2">
              <Clock className="w-4 h-4" />
              Est. Discharge: {new Date(workflow.estimated_discharge_time).toLocaleString()}
            </div>
          )}
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
          <Button
            data-testid="complete-workflow-btn"
            onClick={handleCompleteWorkflow}
            disabled={completingWorkflow}
            className="mt-6 bg-emerald-600 hover:bg-emerald-500"
          >
            {completingWorkflow ? (
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
            ) : (
              <CheckCircle2 className="w-4 h-4 mr-2" />
            )}
            Complete Discharge
          </Button>
        )}
      </div>

      {/* AI Discharge Summary */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <Sparkles className="w-5 h-5 text-blue-400" strokeWidth={1.5} />
            <h2 className="text-lg font-medium text-zinc-50">AI Discharge Summary</h2>
          </div>
          {workflow.discharge_summary?.approved && (
            <span className="flex items-center gap-1 text-emerald-400 text-sm">
              <CheckCircle2 className="w-4 h-4" />
              Approved
            </span>
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
                  {!summary.approved && canApprove && (
                    <div className="mt-4 flex gap-3">
                      <Button
                        data-testid="approve-summary-btn"
                        onClick={handleApproveSummary}
                        disabled={approvingSummary}
                        className="bg-emerald-600 hover:bg-emerald-500"
                      >
                        {approvingSummary ? (
                          <Loader2 className="w-4 h-4 animate-spin mr-2" />
                        ) : (
                          <Check className="w-4 h-4 mr-2" />
                        )}
                        {editingSummary ? 'Approve Edited Summary' : 'Approve Summary'}
                      </Button>
                      <Button
                        data-testid="edit-summary-btn"
                        variant="outline"
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
                disabled={generatingSummary}
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
                      disabled={isUpdating || task.status === 'completed'}
                      onCheckedChange={(checked) => {
                        handleTaskUpdate(task.task_id, checked ? 'completed' : 'pending');
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
                      {task.completed_at && (
                        <span className="text-zinc-600">
                          Completed: {new Date(task.completed_at).toLocaleString()}
                        </span>
                      )}
                    </div>
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
