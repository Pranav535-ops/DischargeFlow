import React, { createContext, useContext, useState, useCallback, useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api, refreshAccessToken, setAccessToken, setClientLocked } from '@/lib/api';
import { LOCK_KEY, broadcastLock, markActivity, readLastActivity, useIdleTimer } from '@/lib/idle';

const AuthContext = createContext(null);

// Where to land after signing in again from the lock screen (same tab, so sessionStorage survives the redirect).
export const RETURN_TO_KEY = 'df:return-to';
const DEFAULT_IDLE_LOCK_SECONDS = 300;

export const AuthProvider = ({ children }) => {
  const queryClient = useQueryClient();
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  // { name } of whoever was signed in when the app locked; null when not locked.
  const [locked, setLocked] = useState(null);
  const [idleLockSeconds, setIdleLockSeconds] = useState(DEFAULT_IDLE_LOCK_SECONDS);
  const refreshTimer = useRef(null);
  const userRef = useRef(null);
  useEffect(() => { userRef.current = user; }, [user]);

  // Shared-workstation lock (ADR-002 item 4). Hides everything and forgets the session in this browser: the
  // refresh session is revoked, so whoever sits down next can't resume it, and the next person has to sign in.
  const lock = useCallback(({ revoke = true } = {}) => {
    setClientLocked(true);
    clearTimeout(refreshTimer.current);
    queryClient.cancelQueries();
    queryClient.clear();
    toast.dismiss(); // toasts can name patients
    setLocked({ name: userRef.current?.name ?? null });
    setUser(null);
    if (revoke) {
      broadcastLock();
      api.post('/auth/logout', { reason: 'idle' }).catch(() => {});
    }
  }, [queryClient]);

  // Refresh a minute before the access token expires so requests rarely see a 401.
  const scheduleRefresh = useCallback((expiresIn) => {
    clearTimeout(refreshTimer.current);
    const delayMs = Math.max((expiresIn - 60) * 1000, 10000);
    refreshTimer.current = setTimeout(async () => {
      try {
        const data = await refreshAccessToken();
        setUser(data.user);
        scheduleRefresh(data.expires_in);
      } catch (e) {
        setAccessToken(null);
        setUser(null);
      }
    }, delayMs);
  }, []);

  const applySession = useCallback((data) => {
    setClientLocked(false);
    setLocked(null);
    setAccessToken(data.token);
    setUser(data.user);
    setIdleLockSeconds(data.idle_lock_seconds || DEFAULT_IDLE_LOCK_SECONDS);
    markActivity();
    scheduleRefresh(data.expires_in);
    return data.user;
  }, [scheduleRefresh]);

  const checkAuth = useCallback(async () => {
    try {
      const data = await refreshAccessToken();
      // The browser was left idle past the lock (closed tab, sleep): lock rather than resume the session.
      const last = readLastActivity();
      const idleMs = (data.idle_lock_seconds || DEFAULT_IDLE_LOCK_SECONDS) * 1000;
      if (last && Date.now() - last >= idleMs) {
        userRef.current = data.user;
        lock();
        return null;
      }
      return applySession(data);
    } catch (error) {
      setAccessToken(null);
      setUser(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, [applySession, lock]);

  useIdleTimer({ timeoutMs: idleLockSeconds * 1000, onIdle: lock, enabled: !!user && !locked });

  useEffect(() => {
    // Tokens from before in-memory storage; no longer valid, so drop them.
    localStorage.removeItem('session_token');

    const onExpired = () => setUser(null);
    window.addEventListener('df:session-expired', onExpired);
    // Another tab locked: lock here too. It already revoked the session.
    const onStorage = (e) => { if (e.key === LOCK_KEY && e.newValue && userRef.current) lock({ revoke: false }); };
    window.addEventListener('storage', onStorage);

    // CRITICAL: If returning from OAuth callback, skip the refresh check.
    // AuthCallback will exchange the session_id and establish the session first.
    if (window.location.hash?.includes('session_id=')) {
      setLoading(false);
    } else {
      checkAuth();
    }
    return () => {
      window.removeEventListener('df:session-expired', onExpired);
      window.removeEventListener('storage', onStorage);
      clearTimeout(refreshTimer.current);
    };
  }, [checkAuth, lock]);

  const login = () => {
    try {
      const here = window.location.pathname + window.location.search;
      if (here.startsWith('/') && !here.startsWith('//') && here !== '/login') sessionStorage.setItem(RETURN_TO_KEY, here);
    } catch {
      // Storage blocked: sign-in lands on the dashboard.
    }
    // REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
    const redirectUrl = window.location.origin + '/dashboard';
    window.location.href = `https://auth.emergentagent.com/?redirect=${encodeURIComponent(redirectUrl)}`;
  };

  const logout = async () => {
    try {
      await api.post('/auth/logout');
    } catch (error) {
      console.error('Logout error:', error);
    }
    clearTimeout(refreshTimer.current);
    setAccessToken(null);
    setUser(null);
    window.location.href = '/login';
  };

  const exchangeSession = async (sessionId) => {
    try {
      const response = await api.post('/auth/session', { session_id: sessionId });
      return applySession(response.data);
    } catch (error) {
      console.error('Session exchange error:', error);
      throw error;
    }
  };

  return (
    <AuthContext.Provider value={{
      user,
      setUser,
      loading,
      login,
      logout,
      checkAuth,
      exchangeSession,
      // A session issued by another sign-in route (the SMART EHR launch, ADR-002 item 14).
      startSession: applySession,
      lock,
      locked,
      idleLockSeconds,
      isAuthenticated: !!user
    }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
