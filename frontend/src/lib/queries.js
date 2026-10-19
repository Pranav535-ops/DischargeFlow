// Server state for the whole app (TanStack Query). Views read through these hooks instead of keeping their own
// copies in useState, so data is cached across navigation, refetched when stale, and refreshed everywhere after a
// mutation by invalidating its key.
import { useEffect } from 'react';
import { QueryClient, useQuery } from '@tanstack/react-query';
import { READ_ONLY_EVENT, api } from '@/lib/api';
import { startNotificationStream, useStreamLive } from '@/lib/notificationStream';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: (count, error) => count < 2 && ![401, 403, 404].includes(error?.response?.status),
      refetchOnWindowFocus: true,
    },
  },
});

const get = (url) => () => api.get(url).then(res => res.data);

// Keys are hierarchical: invalidating ['patients'] also refreshes ['patients', id].
export const keys = {
  patients: ['patients'],
  patient: (id) => ['patients', id],
  patientWorkflow: (id) => ['patients', id, 'workflow'],
  patientEncounters: (id) => ['patients', id, 'encounters'],
  workflow: (id) => ['workflows', id],
  workflows: ['workflows'],
  tasks: ['tasks'],
  myTasks: ['tasks', 'my'],
  notifications: ['notifications'],
  dashboard: ['analytics', 'dashboard'],
  tasksByRole: ['analytics', 'tasks-by-role'],
  aggregates: ['analytics', 'aggregates'],
  operations: (days) => ['analytics', 'operations', days],
  claims: ['insurance', 'claims'],
  insuranceReports: ['insurance', 'reports'],
  users: ['users'],
  units: ['units'],
  accessGrants: ['access-grants'],
  downtime: ['downtime', 'status'],
  downtimePeriods: ['downtime', 'periods'],
  census: (unitId, at) => ['census', unitId || 'all', at],
  escalations: ['escalations'],
  // Discharge plans (ADR-002 items 8-10). Under ['patients'] so a discharge change refreshes them too.
  plan: (encounterId) => ['patients', 'plans', encounterId],
  planReview: (unitId) => ['patients', 'plans', 'review', unitId || 'all'],
  planCodes: ['discharge-plans', 'codes'],
  templates: ['workflow-templates'],
};

export const usePatients = () => useQuery({ queryKey: keys.patients, queryFn: get('/patients') });
export const usePatient = (id) => useQuery({ queryKey: keys.patient(id), queryFn: get(`/patients/${id}`), enabled: !!id });
export const usePatientWorkflow = (id) =>
  useQuery({ queryKey: keys.patientWorkflow(id), queryFn: get(`/workflows/patient/${id}`), enabled: !!id });
export const useEncounters = (id) =>
  useQuery({ queryKey: keys.patientEncounters(id), queryFn: get(`/patients/${id}/encounters`), enabled: !!id });
export const useWorkflow = (id) => useQuery({ queryKey: keys.workflow(id), queryFn: get(`/workflows/${id}`), enabled: !!id });
export const useTasks = () => useQuery({ queryKey: keys.tasks, queryFn: get('/tasks') });
export const useMyTasks = () => useQuery({ queryKey: keys.myTasks, queryFn: get('/tasks/my') });
// Polls every 30 s only while the live stream (useNotificationStream) is down.
export const useNotifications = () => {
  const live = useStreamLive();
  return useQuery({ queryKey: keys.notifications, queryFn: get('/notifications'), refetchInterval: live ? false : 30_000 });
};

// Mounted once in the signed-in layout: each stream event refetches the notification list.
export const useNotificationStream = () => {
  useEffect(() => startNotificationStream({
    onEvent: (event) => {
      if (event === 'ready' || event === 'notification') {
        queryClient.invalidateQueries({ queryKey: keys.notifications });
      }
    },
  }), []);
};
export const useDashboardAnalytics = () => useQuery({ queryKey: keys.dashboard, queryFn: get('/analytics/dashboard') });
export const useTasksByRole = () => useQuery({ queryKey: keys.tasksByRole, queryFn: get('/analytics/tasks-by-role') });
export const useAggregates = () => useQuery({ queryKey: keys.aggregates, queryFn: get('/analytics/aggregates') });
export const useOperations = (days = 30) =>
  useQuery({ queryKey: keys.operations(days), queryFn: get(`/analytics/operations?days=${days}`) });
export const useClaims = () => useQuery({ queryKey: keys.claims, queryFn: get('/insurance/claims') });
export const useInsuranceReports = () => useQuery({ queryKey: keys.insuranceReports, queryFn: get('/insurance/reports') });
export const useUsers = () => useQuery({ queryKey: keys.users, queryFn: get('/users') });
export const useUnits = () => useQuery({ queryKey: keys.units, queryFn: get('/units'), staleTime: 300_000 });
export const useAccessGrants = () => useQuery({ queryKey: keys.accessGrants, queryFn: get('/access-grants') });
// Read-only (downtime) mode, ADR-002 item 6. Polled so every open screen learns of it within 30 s; a write refused
// with 503 refetches it at once.
export const useDowntimeStatus = () =>
  useQuery({ queryKey: keys.downtime, queryFn: get('/downtime/status'), refetchInterval: 30_000, staleTime: 10_000 });
export const useReadOnly = () => useDowntimeStatus().data?.read_only === true;
export const useDowntimePeriods = () => useQuery({ queryKey: keys.downtimePeriods, queryFn: get('/downtime/periods') });
// Paper work can be back-entered when a downtime period exists and the system is writable again.
export const useCanBackfill = () => {
  const readOnly = useReadOnly();
  const { data: periods } = useDowntimePeriods();
  return !readOnly && (periods?.length ?? 0) > 0;
};
// Active escalations of overdue tasks and the policy behind them (ADR-002 item 12), admin screen.
export const useEscalations = () => useQuery({ queryKey: keys.escalations, queryFn: get('/escalations'), refetchInterval: 60_000 });
export const useCensus = (unitId, at) => useQuery({
  queryKey: keys.census(unitId, at),
  queryFn: () => api.get('/census', { params: unitId ? { unit_id: unitId } : {} }).then(res => res.data),
  // Each load is an audited PHI read, so it loads when asked for, not on every focus.
  staleTime: Infinity,
  refetchOnWindowFocus: false,
});
export const usePlan = (encounterId) =>
  useQuery({ queryKey: keys.plan(encounterId), queryFn: get(`/encounters/${encounterId}/plan`), enabled: !!encounterId });
export const usePlanReview = (unitId) => useQuery({
  queryKey: keys.planReview(unitId),
  queryFn: () => api.get('/discharge-plans/review', { params: unitId ? { unit_id: unitId } : {} }).then(res => res.data),
});
export const usePlanCodes = () => useQuery({ queryKey: keys.planCodes, queryFn: get('/discharge-plans/codes'), staleTime: Infinity });
export const useTemplates = () => useQuery({ queryKey: keys.templates, queryFn: get('/workflow-templates') });
if (typeof window !== 'undefined') {
  window.addEventListener(READ_ONLY_EVENT, () => queryClient.invalidateQueries({ queryKey: keys.downtime }));
}

// A patient outside the user's units (403 with break_glass), as opposed to one that does not exist.
export const isBreakGlassError = (error) => error?.response?.status === 403 && error?.response?.data?.break_glass === true;

// After any change to discharge state: workflows, tasks, patients' statuses, notifications and dashboards all move.
export const invalidateDischarge = () => Promise.all(
  [keys.workflows, keys.patients, keys.tasks, keys.notifications, ['analytics']]
    .map(queryKey => queryClient.invalidateQueries({ queryKey })),
);
