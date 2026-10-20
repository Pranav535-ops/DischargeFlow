import React, { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { useAuth } from '../context/AuthContext';
import { takeReturnPath } from './AuthCallback';
import { errorMessage } from '@/lib/api';
import { takeCallback } from '@/lib/sso';

// The identity provider redirects here with ?code=&state= (ADR-002 item 2). The code is redeemed by the backend.
const SsoCallback = () => {
  const navigate = useNavigate();
  const { exchangeSsoCode } = useAuth();
  const handled = useRef(false);

  useEffect(() => {
    if (handled.current) return; // StrictMode runs effects twice; the saved state is single-use
    handled.current = true;
    const { body, error } = takeCallback(window.location.search);
    // The code must not linger in the address bar or history.
    window.history.replaceState(null, '', '/login');
    if (error) {
      toast.error('Sign-in failed', { description: error });
      navigate('/login', { replace: true });
      return;
    }
    exchangeSsoCode(body)
      .then((user) => {
        const target = takeReturnPath();
        window.history.replaceState(null, '', target);
        navigate(target, { state: { user }, replace: true });
      })
      .catch((e) => {
        toast.error('Sign-in refused', { description: errorMessage(e) });
        navigate('/login', { replace: true });
      });
  }, [exchangeSsoCode, navigate]);

  return (
    <div className="min-h-screen bg-zinc-950 flex items-center justify-center">
      <div className="text-center">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500 mx-auto mb-4" />
        <p className="text-zinc-400">Signing you in...</p>
      </div>
    </div>
  );
};

export default SsoCallback;
