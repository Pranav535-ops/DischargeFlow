import React, { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { RETURN_TO_KEY, useAuth } from '../context/AuthContext';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { errorMessage } from '@/lib/api';

// A same-origin path saved by login(), used once; anything else lands on the dashboard.
const takeReturnPath = () => {
  try {
    const path = sessionStorage.getItem(RETURN_TO_KEY);
    sessionStorage.removeItem(RETURN_TO_KEY);
    if (path && path.startsWith('/') && !path.startsWith('//') && !path.startsWith('/\\')) return path;
  } catch {
    // Storage blocked.
  }
  return '/dashboard';
};

const AuthCallback = () => {
  const navigate = useNavigate();
  const { exchangeSession } = useAuth();
  const hasProcessed = useRef(false);

  useEffect(() => {
    // Prevent double execution in StrictMode
    if (hasProcessed.current) return;
    hasProcessed.current = true;

    const processAuth = async () => {
      // Extract session_id from URL hash
      const hash = window.location.hash;
      const params = new URLSearchParams(hash.substring(1));
      const sessionId = params.get('session_id');

      if (!sessionId) {
        console.error('No session_id found');
        navigate('/login');
        return;
      }

      // Safeguard against React StrictMode Double-execution: the first run owns the exchange.
      if (sessionStorage.getItem(`auth_processing_${sessionId}`)) {
        return;
      }
      sessionStorage.setItem(`auth_processing_${sessionId}`, 'true');

      try {
        const user = await exchangeSession(sessionId);
        // Clear the hash and redirect, back to the screen the lock screen was covering if there was one.
        const target = takeReturnPath();
        window.history.replaceState(null, '', target);
        navigate(target, { state: { user }, replace: true });
      } catch (error) {
        console.error('Auth failed:', error);
        // 403: the account is not invited or its email domain is not allowed (identity/router.registration_refusal).
        if (error?.response?.status === 403) toast.error('Sign-in refused', { description: errorMessage(error) });
        navigate('/login');
      }
    };

    processAuth();
  }, [exchangeSession, navigate]);

  return (
    <div className="min-h-screen bg-zinc-950 flex items-center justify-center">
      <div className="text-center">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500 mx-auto mb-4" />
        <p className="text-zinc-400">Authenticating...</p>
      </div>
    </div>
  );
};

export default AuthCallback;
