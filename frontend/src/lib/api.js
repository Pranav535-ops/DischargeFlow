// The one HTTP client for the app (ADR-001 Phase 5): base URL, credentials, bearer token and refresh-on-401.
//
// The access token lives only in memory (never localStorage), so injected scripts can't lift a long-lived
// credential. The refresh token is an httpOnly cookie the browser sends to /api/auth/refresh on its own.
import axios from 'axios';

const BACKEND = import.meta.env.VITE_BACKEND_URL || import.meta.env.REACT_APP_BACKEND_URL || '';

export const API_BASE = `${BACKEND}/api`;

export const api = axios.create({ baseURL: API_BASE, withCredentials: true });

let accessToken = null;
let refreshInFlight = null;

export const setAccessToken = (token) => { accessToken = token; };
// For the few callers that can't go through axios (the notification stream reads a fetch body).
export const getAccessToken = () => accessToken;

const isAuthCall = (url = '') => ['/auth/refresh', '/auth/session', '/auth/logout', '/auth/dev-login']
  .some(path => url.includes(path));

api.interceptors.request.use(config => {
  if (accessToken && !isAuthCall(config.url)) {
    config.headers.set('Authorization', `Bearer ${accessToken}`);
  }
  return config;
});

// Single-flight refresh: concurrent 401s share one /auth/refresh call. Resolves to { token, expires_in, user }.
export const refreshAccessToken = () => {
  if (!refreshInFlight) {
    refreshInFlight = api.post('/auth/refresh')
      .then(res => {
        accessToken = res.data.token;
        return res.data;
      })
      .finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
};

// On a 401 from any API call, refresh once and retry the original request.
api.interceptors.response.use(undefined, async error => {
  const original = error.config;
  if (error.response?.status === 401 && original && !original._retried && !isAuthCall(original.url)) {
    original._retried = true;
    try {
      await refreshAccessToken();
      return api(original);
    } catch {
      accessToken = null;
      window.dispatchEvent(new Event('df:session-expired'));
    }
  }
  return Promise.reject(error);
});

// The message to show for a failed request.
export const errorMessage = (error, fallback = 'Something went wrong.') => {
  const detail = error?.response?.data?.detail;
  return typeof detail === 'string' ? detail : fallback;
};
