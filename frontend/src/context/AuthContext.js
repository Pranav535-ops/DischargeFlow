import React, { createContext, useContext, useState, useCallback, useEffect, useRef } from 'react';
import axios from 'axios';

const AuthContext = createContext(null);

const API = `${process.env.REACT_APP_BACKEND_URL || ""}/api`;

// The access token lives only in memory (never localStorage), so injected scripts can't lift a long-lived
// credential. The refresh token is an httpOnly cookie the browser sends to /api/auth/refresh on its own.
let accessToken = null;
let refreshInFlight = null;

const isAuthCall = (url = '') => url.includes('/auth/refresh') || url.includes('/auth/session') || url.includes('/auth/logout');

axios.interceptors.request.use(config => {
  if (accessToken && !isAuthCall(config.url)) {
    if (config.headers && typeof config.headers.set === 'function') {
      config.headers.set('Authorization', `Bearer ${accessToken}`);
    } else {
      config.headers = { ...config.headers, Authorization: `Bearer ${accessToken}` };
    }
  }
  return config;
});

// Single-flight refresh: concurrent 401s share one /auth/refresh call.
const refreshAccessToken = () => {
  if (!refreshInFlight) {
    refreshInFlight = axios.post(`${API}/auth/refresh`, {}, { withCredentials: true })
      .then(res => {
        accessToken = res.data.token;
        return res.data;
      })
      .finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
};

// On a 401 from any API call, refresh once and retry the original request.
axios.interceptors.response.use(undefined, async error => {
  const original = error.config;
  if (error.response?.status === 401 && original && !original._retried && !isAuthCall(original.url)) {
    original._retried = true;
    try {
      await refreshAccessToken();
      return axios(original);
    } catch (refreshError) {
      accessToken = null;
      window.dispatchEvent(new Event('df:session-expired'));
    }
  }
  return Promise.reject(error);
});

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
        accessToken = null;
        setUser(null);
      }
    }, delayMs);
  }, []);

  const applySession = useCallback((data) => {
    accessToken = data.token;
    setUser(data.user);
    scheduleRefresh(data.expires_in);
    return data.user;
  }, [scheduleRefresh]);

  const checkAuth = useCallback(async () => {
    try {
      return applySession(await refreshAccessToken());
    } catch (error) {
      accessToken = null;
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
      await axios.post(`${API}/auth/logout`, {}, { withCredentials: true });
    } catch (error) {
      console.error('Logout error:', error);
    }
    clearTimeout(refreshTimer.current);
    accessToken = null;
    setUser(null);
    window.location.href = '/login';
  };

  const exchangeSession = async (sessionId) => {
    try {
      const response = await axios.post(`${API}/auth/session`,
        { session_id: sessionId },
        { withCredentials: true }
      );
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
