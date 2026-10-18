// Who may change a task's status, mirroring discharge/service.py:update_task. The API is the authority; this only
// decides which controls to show and when to ask for a reason.

export const isTaskAssignee = (task, user) =>
  Boolean(user) && (task.assigned_to === user.user_id || task.assigned_role === user.role);

// task:override is seeded for physicians and admins: they may change another role's task, with a reason.
export const canOverrideTask = (user) => ['physician', 'admin'].includes(user?.role);

export const canChangeTaskStatus = (task, user) => isTaskAssignee(task, user) || canOverrideTask(user);

// The PUT /tasks/:id body for a status change, asking for the override reason when the user is not the assignee.
// Returns null if they cancel.
export const taskStatusBody = (task, user, status, ask = window.prompt) => {
  if (isTaskAssignee(task, user)) return { status };
  const reason = ask(`"${task.title}" is assigned to the ${task.assigned_role} team. Why are you changing it?`);
  if (!reason || !reason.trim()) return null;
  return { status, reason: reason.trim() };
};
