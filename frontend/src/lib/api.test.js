import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, errorMessage, setAccessToken } from './api';

// Replaces the network: each call to the adapter returns the next scripted response.
const script = (responses) => {
  const calls = [];
  api.defaults.adapter = async (config) => {
    calls.push({ url: config.url, auth: config.headers.get('Authorization') ?? null });
    const next = responses.shift();
    const response = { data: next.data ?? {}, status: next.status, statusText: '', headers: {}, config };
    if (next.status >= 400) {
      const error = new Error(`status ${next.status}`);
      Object.assign(error, { config, response, isAxiosError: true });
      throw error;
    }
    return response;
  };
  return calls;
};

afterEach(() => setAccessToken(null));

describe('api client', () => {
  it('sends the in-memory access token as a bearer header', async () => {
    const calls = script([{ status: 200, data: [] }]);
    setAccessToken('t1');
    await api.get('/patients');
    expect(calls).toEqual([{ url: '/patients', auth: 'Bearer t1' }]);
  });

  it('refreshes once on 401 and retries with the new token', async () => {
    const calls = script([
      { status: 401 },
      { status: 200, data: { token: 't2', expires_in: 900, user: {} } },
      { status: 200, data: ['ok'] },
    ]);
    setAccessToken('expired');
    const res = await api.get('/patients');
    expect(res.data).toEqual(['ok']);
    expect(calls.map(c => [c.url, c.auth])).toEqual([
      ['/patients', 'Bearer expired'],
      ['/auth/refresh', null], // the refresh call relies on the httpOnly cookie, never a bearer token
      ['/patients', 'Bearer t2'],
    ]);
  });

  it('announces an expired session when the refresh fails', async () => {
    script([{ status: 401 }, { status: 401, data: { detail: 'Session expired' } }]);
    const expired = vi.fn();
    window.addEventListener('df:session-expired', expired);
    await expect(api.get('/patients')).rejects.toThrow();
    expect(expired).toHaveBeenCalledOnce();
    window.removeEventListener('df:session-expired', expired);
  });

  it('reads FastAPI error details', () => {
    expect(errorMessage({ response: { data: { detail: 'Patient not found' } } })).toBe('Patient not found');
    expect(errorMessage({ response: { data: { detail: [{ msg: 'x' }] } } }, 'fallback')).toBe('fallback');
  });
});
