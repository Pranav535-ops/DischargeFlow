// Capacity board helpers (ADR-002 item 17).

// Hours shown on the board's strip; discharges outside them still count in the unit's totals.
export const BOARD_HOURS = Array.from({ length: 15 }, (_, i) => i + 7); // 07:00 .. 21:00
// The next step for a bed, if any: dirty -> cleaning -> available.
export const NEXT_BED_STATUS = { dirty: 'cleaning', cleaning: 'available' };
// Matches bed:manage for people (nurse and admin); the EVS system uses an API key.
export const canManageBeds = (user) => ['nurse', 'admin'].includes(user?.role);
export const percent = (rate) => (rate == null ? null : `${Math.round(rate * 100)}%`);
