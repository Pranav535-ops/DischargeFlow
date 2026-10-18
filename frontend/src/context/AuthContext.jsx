import React, { createContext, useContext, useState, useCallback, useEffect, useRef } from 'react';
import { api, refreshAccessToken, setAccessToken } from '@/lib/api';

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const refreshTimer = useRef(null);

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
    setAccessToken(data.token);
    setUser(data.user);
    scheduleRefresh(data.expires_in);
    return data.user;
  }, [scheduleRefresh]);

  const checkAuth = useCallback(async () => {
    try {
      return applySession(await refreshAccessToken());
    } catch (error) {
      setAccessToken(null);
      setUser(null);
      return null;
    } finally {
      setLoading(false);
    }
  }, [applySession]);

  useEffect(() => {
    // Tokens from before in-memory storage; no longer valid, so drop them.
    localStorage.removeItem('session_token');

    const onExpired = () => setUser(null);
    window.addEventListener('df:session-expired', onExpired);

    // CRITICAL: If returning from OAuth callback, skip the refresh check.
    // AuthCallback will exchange the session_id and establish the session first.
    if (window.location.hash?.includes('session_id=')) {
      setLoading(false);
    } else {
      checkAuth();
    }
    return () => {
      window.removeEventListener('df:session-expired', onExpired);
      clearTimeout(refreshTimer.current);
    };
  }, [checkAuth]);

  const login = () => {
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
