// Who may change a task's status, mirroring discharge/service.py:update_task. The API is the authority; this only
// decides which controls to show and when to ask for a reason.

export const isTaskAssignee = (task, user) =>
  Boolean(user) && (task.assigned_to === user.user_id || task.assigned_role === user.role);

// task:override is seeded for physicians and admins: they may change another role's task, with a reason.
export const canOverrideTask = (user) => ['physician', 'admin'].includes(user?.role);

export const canChangeTaskStatus = (task, user) => isTaskAssignee(task, user) || canOverrideTask(user);

// Delay reason codes (ADR-002 item 10), as discharge/models.py:DELAY_CODES.
export const DELAY_CODES = ['medical', 'social', 'placement', 'insurance', 'transport', 'pharmacy', 'documentation', 'other'];

// Ask why something is late: returns {delay_code, delay_note?}, or null if cancelled or not a known code. The note
// is optional except for `other`.
export const askDelayReason = (question, ask = window.prompt) => {
  const answer = ask(`${question}\nDelay reason: ${DELAY_CODES.join(', ')}\n`
    + '(optionally followed by a note, e.g. "placement waiting for a bed")');
  if (!answer || !answer.trim()) return null;
  const [first, ...rest] = answer.trim().split(/\s+/);
  const code = first.toLowerCase();
  if (!DELAY_CODES.includes(code)) return null;
  const note = rest.join(' ').trim();
  if (code === 'other' && !note) return null;
  return note ? { delay_code: code, delay_note: note } : { delay_code: code };
};

// A prerequisite (ADR-002 item 9) is still open: the task can't start or be completed yet.
export const isBlocked = (task) => (task.blocked_by?.length ?? 0) > 0;

// Completing a task past its due time needs a delay reason, unless one is on record (the API answers 400 then).
const needsDelayReason = (task, status) => status === 'completed' && task.overdue && !task.delay_recorded;

// The PUT /tasks/:id body for a status change, asking for the override reason when the user is not the assignee, and
// the delay reason when a late task is completed. Returns null if they cancel.
export const taskStatusBody = (task, user, status, ask = window.prompt) => {
  let body = { status };
  if (!isTaskAssignee(task, user)) {
    const reason = ask(`"${task.title}" is assigned to the ${task.assigned_role} team. Why are you changing it?`);
    if (!reason || !reason.trim()) return null;
    body = { ...body, reason: reason.trim() };
  }
  if (needsDelayReason(task, status)) {
    const delay = askDelayReason(`"${task.title}" is past its due time.`, ask);
    if (!delay) return null;
    body = { ...body, ...delay };
  }
  return body;
};

// The PUT /tasks/:id/backfill body for a task done on paper during a downtime. `localTime` is a datetime-local input
// value, read as the browser's local time and sent with its offset (UTC). Returns null if cancelled or invalid.
export const taskBackfillBody = (task, user, localTime, ask = window.prompt) => {
  const when = localTime ? new Date(localTime) : null;
  if (!when || Number.isNaN(when.getTime())) return null;
  const completed_at = when.toISOString();
  let body = { completed_at };
  if (!isTaskAssignee(task, user)) {
    const reason = ask(`"${task.title}" is assigned to the ${task.assigned_role} team. Why are you entering it?`);
    if (!reason || !reason.trim()) return null;
    body = { ...body, reason: reason.trim() };
  }
  if (task.due && when > new Date(task.due) && !task.delay_recorded) {
    const delay = askDelayReason(`"${task.title}" was done after its due time.`, ask);
    if (!delay) return null;
    body = { ...body, ...delay };
  }
  return body;
};
