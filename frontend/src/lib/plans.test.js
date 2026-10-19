import { describe, expect, it, vi } from 'vitest';
import { planReviewBody, planUpdateBody } from './plans';
import { askDelayReason, taskBackfillBody, taskStatusBody } from './tasks';

const plan = { expected_discharge_date: '2026-10-20', planned_disposition: 'home', reviewed_today: false };
const nurse = { user_id: 'user_n', role: 'nurse' };

describe('delay reasons', () => {
  it('reads a code and an optional note, and refuses unknown codes or a bare "other"', () => {
    expect(askDelayReason('Late', () => 'Placement waiting for a bed'))
      .toEqual({ delay_code: 'placement', delay_note: 'waiting for a bed' });
    expect(askDelayReason('Late', () => ' transport ')).toEqual({ delay_code: 'transport' });
    expect(askDelayReason('Late', () => 'weather')).toBeNull();
    expect(askDelayReason('Late', () => 'other')).toBeNull();
    expect(askDelayReason('Late', () => null)).toBeNull();
  });

  it('asks when a late task is completed, unless a reason is on record', () => {
    const late = { task_id: 't', title: 'Arrange Transport', assigned_role: 'nurse', overdue: true };
    expect(taskStatusBody(late, nurse, 'completed', () => 'transport')).toEqual({ status: 'completed', delay_code: 'transport' });
    expect(taskStatusBody(late, nurse, 'completed', () => '')).toBeNull();
    const ask = vi.fn();
    expect(taskStatusBody({ ...late, delay_recorded: true }, nurse, 'completed', ask)).toEqual({ status: 'completed' });
    expect(taskStatusBody(late, nurse, 'in_progress', ask)).toEqual({ status: 'in_progress' });
    expect(ask).not.toHaveBeenCalled();
  });

  it('asks when paper work is entered after the due time', () => {
    const task = { task_id: 't', title: 'Vitals', assigned_role: 'nurse', due: '2026-10-19T08:00:00Z' };
    expect(taskBackfillBody(task, nurse, '2026-10-19T23:00', () => 'documentation')).toMatchObject({ delay_code: 'documentation' });
  });
});

describe('plan edits', () => {
  it('sends only what changed and asks for a reason when the date moves later', () => {
    const ask = vi.fn();
    expect(planUpdateBody(plan, { expected_discharge_date: '2026-10-20', planned_disposition: 'home' }, ask)).toBeNull();
    expect(planUpdateBody(plan, { expected_discharge_date: '2026-10-19', planned_disposition: 'snf' }, ask))
      .toEqual({ expected_discharge_date: '2026-10-19', planned_disposition: 'snf' });
    expect(planUpdateBody(plan, { expected_discharge_date: '', planned_disposition: 'home' }, ask))
      .toEqual({ expected_discharge_date: null });
    expect(ask).not.toHaveBeenCalled();
    expect(planUpdateBody(plan, { expected_discharge_date: '2026-10-23', planned_disposition: 'home' }, () => 'insurance'))
      .toEqual({ expected_discharge_date: '2026-10-23', delay_code: 'insurance' });
    expect(planUpdateBody(plan, { expected_discharge_date: '2026-10-23', planned_disposition: 'home' }, () => null)).toBeNull();
  });

  it('asks for a reason on the first review after the date has passed', () => {
    const ask = vi.fn(() => 'social');
    expect(planReviewBody(plan, ask)).toEqual({});
    expect(planReviewBody({ ...plan, expected_date_passed: true }, ask)).toEqual({ delay_code: 'social' });
    expect(planReviewBody({ ...plan, expected_date_passed: true, reviewed_today: true }, ask)).toEqual({});
  });
});
