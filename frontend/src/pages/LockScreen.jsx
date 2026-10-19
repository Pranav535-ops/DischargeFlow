import React from 'react';
import { Lock } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import { Button } from '../components/ui/button';

// Shown instead of the whole app once it locks for inactivity (ADR-002 item 4). Nothing behind it is mounted, the
// query cache and access token are gone, and the session is revoked, so signing in again is the only way back.
export const LockScreen = () => {
  const { locked, login, idleLockSeconds } = useAuth();
  const minutes = Math.round(idleLockSeconds / 60);
  return (
    <div className="min-h-screen bg-zinc-950 flex items-center justify-center p-4" data-testid="lock-screen">
      <div className="w-full max-w-sm rounded-xl border border-zinc-800 bg-zinc-900 p-8 text-center">
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-zinc-800">
          <Lock className="h-6 w-6 text-blue-400" />
        </div>
        <h1 className="text-xl font-semibold text-zinc-100">Session locked</h1>
        <p className="mt-2 text-sm text-zinc-400">
          DischargeFlow locked after {minutes} {minutes === 1 ? 'minute' : 'minutes'} without activity and hid
          all patient information.
          {locked?.name && <> {locked.name} was signed in.</>}
        </p>
        <Button className="mt-6 w-full" onClick={login} data-testid="unlock-sign-in-btn">
          Sign in again
        </Button>
        <p className="mt-3 text-xs text-zinc-500">Anyone can sign in here with their own account.</p>
      </div>
    </div>
  );
};

// Renders the lock screen in place of its children while the app is locked.
export const LockGate = ({ children }) => {
  const { locked } = useAuth();
  return locked ? <LockScreen /> : children;
};

export default LockScreen;
