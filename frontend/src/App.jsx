import React, { lazy, Suspense } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { QueryClientProvider } from '@tanstack/react-query';
import { AuthProvider, useAuth } from './context/AuthContext';
import { queryClient } from './lib/queries';
import { Toaster } from './components/ui/sonner';
import LoginPage from './pages/LoginPage';
import AuthCallback from './pages/AuthCallback';
import { LockGate } from './pages/LockScreen';
import AppLayout from './pages/AppLayout';
import PendingApproval from './pages/PendingApproval';
import { Loader2 } from 'lucide-react';

// Each screen is its own chunk, loaded on first visit.
const HomeView = lazy(() => import('./components/HomeView'));
const PatientList = lazy(() => import('./components/PatientList'));
const PatientDetails = lazy(() => import('./components/PatientDetails'));
const WorkflowView = lazy(() => import('./components/WorkflowView'));
const TasksView = lazy(() => import('./components/TasksView'));
const InsuranceView = lazy(() => import('./components/InsuranceView'));
const ChatbotView = lazy(() => import('./components/ChatbotView'));
const AnalyticsView = lazy(() => import('./components/AnalyticsView'));
const AdminView = lazy(() => import('./components/AdminView'));
const InsightsView = lazy(() => import('./components/InsightsView'));
const CensusView = lazy(() => import('./components/CensusView'));
const PlanReviewView = lazy(() => import('./components/PlanReviewView'));
const CapacityBoardView = lazy(() => import('./components/CapacityBoardView'));

export const Spinner = ({ full = false }) => (
  <div className={`${full ? 'min-h-screen bg-zinc-950' : 'h-64'} flex items-center justify-center`}>
    <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
  </div>
);

// Protected Route Component
export const ProtectedRoute = ({ children }) => {
  const { user, loading } = useAuth();
  // No shortcut via router state: it survives reloads, and rendering before the session refresh finishes
  // makes the first API calls fail with 401.
  if (loading) return <Spinner full />;
  if (!user) return <Navigate to="/login" replace />;
  // Signed in but not yet approved: the API refuses every screen's data, so show the waiting screen instead.
  if (user.role === 'pending') return <PendingApproval />;
  return children;
};

// Screens limited to some roles. The API enforces permissions; this only keeps people off pages they can't use.
const RoleRoute = ({ roles, children }) => {
  const { user } = useAuth();
  return roles.includes(user?.role) ? children : <Navigate to="/dashboard" replace />;
};

// Public Route - redirects to dashboard if already logged in
const PublicRoute = ({ children }) => {
  const { user, loading } = useAuth();
  if (loading) return <Spinner full />;
  if (user) return <Navigate to="/dashboard" replace />;
  return children;
};

const page = (element) => <Suspense fallback={<Spinner />}>{element}</Suspense>;

// App Router with session_id detection
const AppRouter = () => {
  const location = useLocation();

  // Check URL fragment (not query params) for session_id synchronously during render
  // This prevents race conditions by processing a new session_id FIRST, before refreshing an existing session
  if (location.hash?.includes('session_id=')) {
    return <AuthCallback />;
  }

  return (
    <Routes>
      <Route path="/login" element={<PublicRoute><LoginPage /></PublicRoute>} />
      <Route element={<ProtectedRoute><AppLayout /></ProtectedRoute>}>
        <Route path="/dashboard" element={page(<HomeView />)} />
        <Route path="/patients" element={page(<PatientList />)} />
        <Route path="/patients/:patientId" element={page(<PatientDetails />)} />
        <Route path="/workflows/:workflowId" element={page(<WorkflowView />)} />
        <Route path="/tasks" element={page(<TasksView />)} />
        <Route path="/insurance" element={page(<InsuranceView />)} />
        <Route path="/assistant" element={page(<ChatbotView />)} />
        <Route path="/census" element={page(<CensusView />)} />
        <Route path="/plans" element={page(<PlanReviewView />)} />
        <Route path="/capacity" element={
          <RoleRoute roles={['physician', 'nurse', 'admin']}>{page(<CapacityBoardView />)}</RoleRoute>} />
        <Route path="/analytics" element={
          <RoleRoute roles={['physician', 'nurse', 'admin']}>{page(<AnalyticsView />)}</RoleRoute>} />
        <Route path="/admin" element={<RoleRoute roles={['admin']}>{page(<AdminView />)}</RoleRoute>} />
        <Route path="/insights" element={<RoleRoute roles={['admin']}>{page(<InsightsView />)}</RoleRoute>} />
      </Route>
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  );
};

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter>
          <div className="dark">
            {/* Locked for inactivity: nothing below is mounted (ADR-002 item 4). */}
            <LockGate><AppRouter /></LockGate>
            <Toaster position="top-right" theme="dark" />
          </div>
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  );
}

export default App;
