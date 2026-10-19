// Idle detection for shared workstations (ADR-002 item 4).
//
// Input in any tab of the app counts: the last activity time is shared through localStorage (a timestamp, nothing
// else), so someone working in one tab doesn't get locked out by another tab they left open. Locking is shared the
// same way: when one tab locks, every tab locks.
import { useEffect, useRef } from 'react';

export const LAST_ACTIVITY_KEY = 'df:last-activity';
export const LOCK_KEY = 'df:locked-at';

const ACTIVITY_EVENTS = ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart', 'scroll'];

export const readLastActivity = () => {
  try {
    const value = Number(localStorage.getItem(LAST_ACTIVITY_KEY));
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
};

export const markActivity = (now = Date.now()) => {
  try {
    localStorage.setItem(LAST_ACTIVITY_KEY, String(now));
  } catch {
    // Storage blocked: idle detection still works for this tab alone.
  }
};

// Tells the other tabs to lock (they receive a storage event; this tab doesn't).
export const broadcastLock = () => {
  try {
    localStorage.setItem(LOCK_KEY, String(Date.now()));
  } catch {
    // Storage blocked: other tabs lock on their own timers.
  }
};

// Calls onIdle once no tab has seen input for timeoutMs. Input that arrives after the deadline (the machine slept,
// or a background tab's timer ran late) locks instead of extending the session.
export function useIdleTimer({ timeoutMs, onIdle, enabled = true }) {
  const onIdleRef = useRef(onIdle);
  useEffect(() => { onIdleRef.current = onIdle; }, [onIdle]);

  useEffect(() => {
    if (!enabled || !timeoutMs) return undefined;
    let last = Date.now();
    let lastWrite = last;
    let timer = null;
    let fired = false;
    markActivity(last);

    const latest = () => Math.max(last, readLastActivity() ?? 0);
    const fire = () => {
      if (fired) return;
      fired = true;
      clearTimeout(timer);
      onIdleRef.current?.();
    };
    const check = () => {
      const remaining = latest() + timeoutMs - Date.now();
      if (remaining <= 0) return fire();
      clearTimeout(timer);
      timer = setTimeout(check, remaining);
    };
    const onActivity = () => {
      const now = Date.now();
      if (now - latest() >= timeoutMs) return fire();
      last = now;
      if (now - lastWrite >= 1000) { // pointermove fires constantly; one write a second is plenty
        lastWrite = now;
        markActivity(now);
      }
    };
    const onVisible = () => { if (document.visibilityState === 'visible') check(); };

    ACTIVITY_EVENTS.forEach(e => window.addEventListener(e, onActivity, { passive: true, capture: true }));
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', check);
    timer = setTimeout(check, timeoutMs);
    return () => {
      clearTimeout(timer);
      ACTIVITY_EVENTS.forEach(e => window.removeEventListener(e, onActivity, { capture: true }));
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', check);
    };
  }, [enabled, timeoutMs]);
}
