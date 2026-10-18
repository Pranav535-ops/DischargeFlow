import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { api, getAccessToken, setClientLocked } from '@/lib/api';
import { LAST_ACTIVITY_KEY, LOCK_KEY, useIdleTimer } from '@/lib/idle';
import { AuthProvider, useAuth } from './AuthContext';
import { LockGate } from '../pages/LockScreen';

const MIN = 60_000;

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  localStorage.clear();
  sessionStorage.clear();
  setClientLocked(false);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('useIdleTimer', () => {
  it('fires after the timeout, and input pushes the deadline back', () => {
    const onIdle = vi.fn();
    renderHook(() => useIdleTimer({ timeoutMs: 5 * MIN, onIdle }));

    act(() => vi.advanceTimersByTime(4 * MIN));
    fireEvent.keyDown(window);
    act(() => vi.advanceTimersByTime(4 * MIN));
    expect(onIdle).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(1 * MIN + 1000));
    expect(onIdle).toHaveBeenCalledTimes(1);
  });

  it('counts input in another tab', () => {
    const onIdle = vi.fn();
    renderHook(() => useIdleTimer({ timeoutMs: 5 * MIN, onIdle }));
    act(() => vi.advanceTimersByTime(4 * MIN));
    localStorage.setItem(LAST_ACTIVITY_KEY, String(Date.now())); // the other tab writes this on input
    act(() => vi.advanceTimersByTime(4 * MIN));
    expect(onIdle).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(2 * MIN));
    expect(onIdle).toHaveBeenCalledTimes(1);
  });

  it('locks instead of resuming when input arrives after the deadline (machine slept, timer late)', () => {
    const onIdle = vi.fn();
    renderHook(() => useIdleTimer({ timeoutMs: 5 * MIN, onIdle }));
    vi.setSystemTime(Date.now() + 30 * MIN); // the clock jumps; no timer has run
    fireEvent.pointerMove(window);
    expect(onIdle).toHaveBeenCalledTimes(1);
  });

  it('does nothing while disabled', () => {
    const onIdle = vi.fn();
    renderHook(() => useIdleTimer({ timeoutMs: 5 * MIN, onIdle, enabled: false }));
    act(() => vi.advanceTimersByTime(30 * MIN));
    expect(onIdle).not.toHaveBeenCalled();
  });
});

const session = (over = {}) => ({
  data: {
    token: 'access-1', expires_in: 900, idle_lock_seconds: 300,
    user: { user_id: 'u1', name: 'Nurse Joy', role: 'nurse' }, ...over,
  },
});

const PatientName = () => {
  const { data } = useQuery({ queryKey: ['patients'], queryFn: async () => (await api.get('/patients')).data });
  return <p>{data ? data[0].name : 'loading'}</p>;
};

// Like ProtectedRoute: screens render only once the session is restored.
const SignedIn = ({ children }) => (useAuth().user ? children : <p>signed out</p>);

const renderApp = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <LockGate><SignedIn><PatientName /></SignedIn></LockGate>
      </AuthProvider>
    </QueryClientProvider>,
  );
  return client;
};

describe('idle lock', () => {
  it('hides patient data, clears the cache and token, and revokes the session', async () => {
    const post = vi.spyOn(api, 'post').mockImplementation(async (url) => {
      if (url === '/auth/refresh') return session();
      return { data: { success: true } };
    });
    const get = vi.spyOn(api, 'get').mockResolvedValue({ data: [{ name: 'Jane Doe' }] });
    const client = renderApp();

    expect(await screen.findByText('Jane Doe')).toBeInTheDocument();
    expect(getAccessToken()).toBe('access-1');

    await act(() => vi.advanceTimersByTimeAsync(5 * MIN + 1000));

    expect(screen.getByTestId('lock-screen')).toHaveTextContent('Nurse Joy was signed in');
    expect(screen.queryByText('Jane Doe')).not.toBeInTheDocument();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    expect(getAccessToken()).toBeNull();
    expect(post).toHaveBeenCalledWith('/auth/logout', { reason: 'idle' });
    expect(localStorage.getItem(LOCK_KEY)).not.toBeNull(); // other tabs lock too

    // Nothing but sign-in calls go out while locked.
    get.mockRestore();
    await expect(api.get('/patients')).rejects.toThrow('Session locked');
  });

  it('stays unlocked while the user keeps working', async () => {
    vi.spyOn(api, 'post').mockImplementation(async (url) => (url === '/auth/refresh' ? session() : { data: {} }));
    vi.spyOn(api, 'get').mockResolvedValue({ data: [{ name: 'Jane Doe' }] });
    renderApp();
    expect(await screen.findByText('Jane Doe')).toBeInTheDocument();
    for (let i = 0; i < 4; i += 1) {
      await act(() => vi.advanceTimersByTimeAsync(4 * MIN));
      fireEvent.keyDown(window);
    }
    expect(screen.queryByTestId('lock-screen')).not.toBeInTheDocument();
  });

  it('locks on load when the browser was left idle past the timeout', async () => {
    localStorage.setItem(LAST_ACTIVITY_KEY, String(Date.now() - 10 * MIN));
    const post = vi.spyOn(api, 'post').mockImplementation(async (url) => (url === '/auth/refresh' ? session() : { data: {} }));
    const get = vi.spyOn(api, 'get').mockResolvedValue({ data: [{ name: 'Jane Doe' }] });
    renderApp();

    expect(await screen.findByTestId('lock-screen')).toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith('/auth/logout', { reason: 'idle' });
  });

  it('locks when another tab locks', async () => {
    vi.spyOn(api, 'post').mockImplementation(async (url) => (url === '/auth/refresh' ? session() : { data: {} }));
    vi.spyOn(api, 'get').mockResolvedValue({ data: [{ name: 'Jane Doe' }] });
    renderApp();
    expect(await screen.findByText('Jane Doe')).toBeInTheDocument();

    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: LOCK_KEY, newValue: String(Date.now()) })); });
    expect(screen.getByTestId('lock-screen')).toBeInTheDocument();
    expect(screen.queryByText('Jane Doe')).not.toBeInTheDocument();
  });
});
