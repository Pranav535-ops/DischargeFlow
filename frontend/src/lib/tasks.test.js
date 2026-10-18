import { describe, expect, it, vi } from 'vitest';
import { canChangeTaskStatus, isTaskAssignee, taskStatusBody } from './tasks';

const nurse = { user_id: 'user_n', role: 'nurse' };
const physician = { user_id: 'user_p', role: 'physician' };
const pharmacist = { user_id: 'user_ph', role: 'pharmacist' };
const nurseTask = { task_id: 'task_1', title: 'Final Vitals Check', assigned_role: 'nurse', assigned_to: null };

describe('task status permissions', () => {
  it('lets the assigned role or named user change the status', () => {
    expect(isTaskAssignee(nurseTask, nurse)).toBe(true);
    expect(isTaskAssignee({ ...nurseTask, assigned_to: 'user_ph' }, pharmacist)).toBe(true);
    expect(canChangeTaskStatus(nurseTask, pharmacist)).toBe(false);
    expect(canChangeTaskStatus(nurseTask, physician)).toBe(true); // override, with a reason
  });

  it('sends a plain status for the assignee without asking', () => {
    const ask = vi.fn();
    expect(taskStatusBody(nurseTask, nurse, 'completed', ask)).toEqual({ status: 'completed' });
    expect(ask).not.toHaveBeenCalled();
  });

  it('asks an overriding physician for a reason, and cancels without one', () => {
    expect(taskStatusBody(nurseTask, physician, 'completed', () => '  Done on the ward round ')).toEqual({
      status: 'completed', reason: 'Done on the ward round',
    });
    expect(taskStatusBody(nurseTask, physician, 'completed', () => '   ')).toBeNull();
    expect(taskStatusBody(nurseTask, physician, 'completed', () => null)).toBeNull();
  });
});
