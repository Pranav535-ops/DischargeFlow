// Live notifications over Server-Sent Events (ADR-001 Phase 6, item 21).
//
// Read with fetch rather than EventSource so the access token goes in the Authorization header, never in a URL.
// Each event only says "something changed": the caller invalidates the notifications query, which refetches the
// list through the normal API. The server ends each stream after NOTIFICATION_STREAM_SECONDS (25 s on Vercel) and
// this loop reconnects; a second `ready` mid-stream (the server's LISTEN connection came back) also refetches. After
// a failure it backs off, and useNotifications polls every 30 s whenever the stream is not live.
import { useSyncExternalStore } from 'react';
import { API_BASE, getAccessToken, refreshAccessToken } from '@/lib/api';

const MAX_BACKOFF_MS = 60_000;

let live = false;
const listeners = new Set();
const setLive = (value) => {
  if (live !== value) {
    live = value;
    listeners.forEach(fn => fn());
  }
};
const subscribe = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };

// True while a stream is connected; useNotifications stops polling then.
export const useStreamLive = () => useSyncExternalStore(subscribe, () => live, () => false);

const sleep = (ms, signal) => new Promise(resolve => {
  const timer = setTimeout(resolve, ms);
  signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
});

// Parse an SSE body, calling onEvent(eventName, data) per frame. Comment lines (keep-alives) are skipped.
export const readEvents = async (body, onEvent) => {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
    let split;
    while ((split = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      let event = 'message';
      const data = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).trim());
      }
      if (data.length) {
        let parsed = null;
        try { parsed = JSON.parse(data.join('\n')); } catch { /* not JSON: pass null */ }
        onEvent(event, parsed);
      }
    }
  }
};

// Runs until the returned stop function is called. `fetchImpl` is injectable for tests.
export const startNotificationStream = ({ onEvent, fetchImpl = fetch }) => {
  const controller = new AbortController();
  const { signal } = controller;

  const loop = async () => {
    let failures = 0;
    let refreshed = false;
    while (!signal.aborted) {
      try {
        const res = await fetchImpl(`${API_BASE}/notifications/stream`, {
          headers: { Accept: 'text/event-stream', Authorization: `Bearer ${getAccessToken()}` },
          credentials: 'include',
          signal,
        });
        if (res.status === 401 && !refreshed) {
          refreshed = true;
          await refreshAccessToken(); // rejects if the session is gone; handled below like any failure
          continue;
        }
        if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
        refreshed = false;
        failures = 0;
        setLive(true);
        let ended = null;
        await readEvents(res.body, (event, data) => {
          if (event === 'end') ended = data?.reason;
          onEvent(event, data);
        });
        setLive(false);
        if (ended === 'session_inactive') return;
      } catch {
        setLive(false);
        if (signal.aborted) return;
        failures += 1;
        refreshed = false;
        await sleep(Math.min(MAX_BACKOFF_MS, 1000 * 2 ** failures), signal);
      }
    }
  };

  loop();
  return () => {
    controller.abort();
    setLive(false);
  };
};

