// Server state for the whole app (TanStack Query). Views read through these hooks instead of keeping their own
// copies in useState, so data is cached across navigation, refetched when stale, and refreshed everywhere after a
// mutation by invalidating its key.
import { QueryClient, useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';

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
  workflow: (id) => ['workflows', id],
  workflows: ['workflows'],
  tasks: ['tasks'],
  myTasks: ['tasks', 'my'],
  notifications: ['notifications'],
  dashboard: ['analytics', 'dashboard'],
  tasksByRole: ['analytics', 'tasks-by-role'],
  aggregates: ['analytics', 'aggregates'],
  claims: ['insurance', 'claims'],
  insuranceReports: ['insurance', 'reports'],
  users: ['users'],
};

export const usePatients = () => useQuery({ queryKey: keys.patients, queryFn: get('/patients') });
export const usePatient = (id) => useQuery({ queryKey: keys.patient(id), queryFn: get(`/patients/${id}`), enabled: !!id });
export const usePatientWorkflow = (id) =>
  useQuery({ queryKey: keys.patientWorkflow(id), queryFn: get(`/workflows/patient/${id}`), enabled: !!id });
export const useWorkflow = (id) => useQuery({ queryKey: keys.workflow(id), queryFn: get(`/workflows/${id}`), enabled: !!id });
export const useTasks = () => useQuery({ queryKey: keys.tasks, queryFn: get('/tasks') });
export const useMyTasks = () => useQuery({ queryKey: keys.myTasks, queryFn: get('/tasks/my') });
export const useNotifications = () =>
  useQuery({ queryKey: keys.notifications, queryFn: get('/notifications'), refetchInterval: 30_000 });
export const useDashboardAnalytics = () => useQuery({ queryKey: keys.dashboard, queryFn: get('/analytics/dashboard') });
export const useTasksByRole = () => useQuery({ queryKey: keys.tasksByRole, queryFn: get('/analytics/tasks-by-role') });
export const useAggregates = () => useQuery({ queryKey: keys.aggregates, queryFn: get('/analytics/aggregates') });
export const useClaims = () => useQuery({ queryKey: keys.claims, queryFn: get('/insurance/claims') });
export const useInsuranceReports = () => useQuery({ queryKey: keys.insuranceReports, queryFn: get('/insurance/reports') });
export const useUsers = () => useQuery({ queryKey: keys.users, queryFn: get('/users') });

// After any change to discharge state: workflows, tasks, patients' statuses, notifications and dashboards all move.
export const invalidateDischarge = () => Promise.all(
  [keys.workflows, keys.patients, keys.tasks, keys.notifications, ['analytics']]
    .map(queryKey => queryClient.invalidateQueries({ queryKey })),
);
