import React, { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertCircle, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorMessage } from '@/lib/api';
import { useAuth } from '../context/AuthContext';
import { challengeFor, landingPath, makeVerifier, saveLaunch, takeLaunch } from '@/lib/smart';

// SMART on FHIR EHR launch (ADR-002 item 14). The EHR opens /launch?iss=...&launch=...; the authorization server
// sends the browser back to /launch?code=...&state=... (or ?error=...). No second sign-in: the EHR's sign-in becomes
// our normal session. The EHR's own tokens stay on the server.
const SmartLaunch = () => {
  const navigate = useNavigate();
  const { startSession } = useAuth();
  const [error, setError] = useState(null);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // StrictMode runs effects twice; a code may be exchanged once only.
    started.current = true;
    const params = new URLSearchParams(window.location.search);

    const begin = async (iss, launch) => {
      const verifier = makeVerifier();
      const { data } = await api.post('/smart/launch', { iss, launch, code_challenge: await challengeFor(verifier) });
      saveLaunch({ state: data.state, verifier });
      window.location.assign(data.authorize_url);
    };

    const finish = async (code, state) => {
      const record = takeLaunch(state);
      // Drop the code from the address bar and history.
      window.history.replaceState(null, '', '/launch');
      if (!record) throw new Error('This sign-in did not start in this tab. Launch DischargeFlow from the EHR again.');
      const { data } = await api.post('/smart/token', { state, code, code_verifier: record.verifier });
      startSession(data);
      if (data.launch?.note && !data.launch.matched) toast.info('Opened from the EHR', { description: data.launch.note });
      navigate(data.user?.role === 'pending' ? '/dashboard' : landingPath(data.launch), { replace: true });
    };

    const run = async () => {
      if (params.get('error')) {
        takeLaunch(null);
        throw new Error(`The EHR did not authorize DischargeFlow (${params.get('error')}).`);
      }
      if (params.get('code')) return finish(params.get('code'), params.get('state'));
      if (params.get('iss') && params.get('launch')) return begin(params.get('iss'), params.get('launch'));
      throw new Error('Open DischargeFlow from the EHR: this page needs the iss and launch parameters.');
    };

    run().catch((e) => setError(e?.response ? errorMessage(e) : e.message));
  }, [navigate, startSession]);

  return (
    <div className="min-h-screen bg-zinc-950 flex items-center justify-center p-6" data-testid="smart-launch">
      {error ? (
        <div className="max-w-md bg-zinc-900 border border-zinc-800 rounded-lg p-6 space-y-3" role="alert">
          <div className="flex items-center gap-2 text-rose-400">
            <AlertCircle className="w-5 h-5" />
            <h1 className="text-lg font-medium">EHR launch failed</h1>
          </div>
          <p className="text-sm text-zinc-300" data-testid="smart-launch-error">{error}</p>
          <a href="/login" className="text-sm text-blue-400 hover:underline">Sign in to DischargeFlow instead</a>
        </div>
      ) : (
        <div className="flex items-center gap-3 text-zinc-400">
          <Loader2 className="w-6 h-6 animate-spin text-blue-500" />
          <span>Opening DischargeFlow from the EHR…</span>
        </div>
      )}
    </div>
  );
};

export default SmartLaunch;
