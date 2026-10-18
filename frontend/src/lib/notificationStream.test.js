import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, setAccessToken } from './api';
import { readEvents, startNotificationStream } from './notificationStream';

const body = (...chunks) => new ReadableStream({
  start(controller) {
    chunks.forEach(c => controller.enqueue(new TextEncoder().encode(c)));
    controller.close();
  },
});

const sseResponse = (...chunks) => ({ ok: true, status: 200, body: body(...chunks) });

afterEach(() => setAccessToken(null));

describe('readEvents', () => {
  it('parses frames split across chunks and skips keep-alives', async () => {
    const events = [];
    await readEvents(
      body('event: ready\ndata: {"retry_ms":5000}\n\n: keep-alive\n\nevent: notif', 'ication\nid: n1\ndata: {"notification_id":"n1"}\n\n'),
      (event, data) => events.push([event, data]),
    );
    expect(events).toEqual([['ready', { retry_ms: 5000 }], ['notification', { notification_id: 'n1' }]]);
  });
});

describe('startNotificationStream', () => {
  it('sends the bearer token and reports each event', async () => {
    setAccessToken('t1');
    const events = [];
    let stop;
    const done = new Promise(resolve => {
      const fetchImpl = vi.fn(async () => sseResponse(
        'event: ready\ndata: {}\n\n',
        'event: notification\ndata: {"notification_id":"n1"}\n\n',
        'event: end\ndata: {"reason":"session_inactive"}\n\n',
      ));
      stop = startNotificationStream({
        fetchImpl,
        onEvent: (event) => { events.push(event); if (event === 'end') resolve(fetchImpl); },
      });
    });
    const fetchImpl = await done;
    stop();
    expect(events).toEqual(['ready', 'notification', 'end']);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe('/api/notifications/stream');
    expect(init.headers.Authorization).toBe('Bearer t1');
  });

  it('refreshes the access token once on a 401, then reconnects with the new one', async () => {
    setAccessToken('expired');
    api.defaults.adapter = async (config) => ({ data: { token: 'fresh' }, status: 200, statusText: '', headers: {}, config });
    const auths = [];
    let stop;
    await new Promise(resolve => {
      const fetchImpl = async (_url, init) => {
        auths.push(init.headers.Authorization);
        if (auths.length === 1) return { ok: false, status: 401, body: null };
        return sseResponse('event: end\ndata: {"reason":"session_inactive"}\n\n');
      };
      stop = startNotificationStream({ fetchImpl, onEvent: (event) => event === 'end' && resolve() });
    });
    stop();
    expect(auths).toEqual(['Bearer expired', 'Bearer fresh']);
  });
});
