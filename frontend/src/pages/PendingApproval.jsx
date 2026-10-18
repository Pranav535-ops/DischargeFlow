import React, { useState } from 'react';
import { Clock, Loader2, LogOut, RefreshCw } from 'lucide-react';
import { Button } from '../components/ui/button';
import { useAuth } from '../context/AuthContext';

// Shown instead of the app while the account's role is `pending`: signed in, but the API refuses everything except
// /auth/me until an admin approves the account and gives it a role.
const PendingApproval = () => {
  const { user, logout, checkAuth } = useAuth();
  const [checking, setChecking] = useState(false);

  const checkAgain = async () => {
    setChecking(true);
    try {
      await checkAuth();
    } finally {
      setChecking(false);
    }
  };

  return (
    <div className="min-h-screen bg-zinc-950 flex items-center justify-center p-4">
      <div data-testid="pending-approval" className="max-w-md w-full bg-zinc-900 border border-zinc-800 rounded-lg p-8 text-center space-y-4">
        <div className="w-12 h-12 mx-auto rounded-full bg-amber-500/10 flex items-center justify-center">
          <Clock className="w-6 h-6 text-amber-400" strokeWidth={1.5} />
        </div>
        <h1 className="text-xl font-semibold text-zinc-50" style={{ fontFamily: 'Outfit, sans-serif' }}>
          Waiting for approval
        </h1>
        <p className="text-sm text-zinc-400">
          You are signed in as <span className="text-zinc-200">{user?.email}</span>. An administrator has been told
          about your request and needs to give your account a role before you can see patient information.
        </p>
        <div className="flex justify-center gap-3 pt-2">
          <Button onClick={checkAgain} disabled={checking} className="bg-blue-600 hover:bg-blue-500">
            {checking ? <Loader2 className="w-4 h-4 mr-1 animate-spin" /> : <RefreshCw className="w-4 h-4 mr-1" />}
            Check again
          </Button>
          <Button variant="outline" onClick={logout} className="border-zinc-700 text-zinc-300">
            <LogOut className="w-4 h-4 mr-1" />
            Sign out
          </Button>
        </div>
      </div>
    </div>
  );
};

export default PendingApproval;
