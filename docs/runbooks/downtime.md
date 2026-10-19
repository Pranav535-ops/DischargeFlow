# Downtime procedure

ADR-002 Phase 7, item 6. Discharges happen around the clock, so when DischargeFlow can't take changes (planned
maintenance, a database restore or failover, an outage) the units carry on from paper and the work is entered
afterwards with the time it was actually done. Three pieces support that:

- **Read-only mode.** Every write answers `503` with "read-only mode for a downtime"; reads, the census, sign-in and
  sign-out keep working. Every screen shows a banner and write controls are disabled.
- **Discharge census** (`/census` in the app, `GET /api/census`): each patient still in hospital, by unit and room,
  with discharge status, estimated discharge, whether the summary is approved, and the open tasks with a tick box.
  Print it or download the CSV. Each load is audited as a PHI read.
- **Backfill.** Afterwards, each open task gets a "Done on paper at" time, and the discharge can be completed with the
  time the patient actually left. The time must fall inside a recorded downtime period.

Roles: the **admin** (anyone holding `downtime:manage`) switches modes and records outages. The **charge nurse** of
each unit prints the census and owns the unit's paper forms. Clinicians record on paper and enter their own work
afterwards.

## Before a planned downtime

1. Announce the window to the units at least a day ahead (time, expected length, who to call).
2. 30 minutes before: each charge nurse opens **Census**, picks the unit and prints it (or downloads the CSV to a
   unit workstation that will stay up). The printout is the paper downtime form: tick each task when it is done and
   write the time and your initials next to it; write discharges at the bottom with the time the patient left.
3. Treat the printouts as patient records: keep them on the unit and shred them after back-entry (step 3 below).

## Switching read-only mode on

Pick one:

- **Normal (the database still works):** Admin screen → **Downtime** → enter a reason everyone can see, with no patient
  details ("Database maintenance until 02:00") → **Start read-only mode**. API: `POST /api/downtime/start
  {"reason": "..."}`. It is stored in the database, so every server (every Vercel instance) refuses writes on its next
  request, and the screens show the banner within 30 seconds. Staff get a notification.
- **Forced (the database must not be written at all, e.g. during a restore):** set `READ_ONLY_MODE=true` in the
  environment and redeploy (Vercel: project settings → Environment Variables; ECS: the task definition). This applies
  to every tenant and does not need the database. Reads still need it, so if the database is down too, the census
  printed beforehand is all there is.

What still works in read-only mode: viewing patients, workflows, tasks and summaries; the census; sign-in, refresh and
sign-out; ending the downtime. What does not: everything that changes data, including the assistant. HL7 ADT messages
get `503` too; the interface engine should queue them and resend afterwards (check that its retry is on).

## During the downtime

- Work from the census printout. New admissions go on paper as well; with the ADT feed queued they arrive by
  themselves once writes are back.
- Reprint the census if the downtime outlasts a shift (reads still work).
- If someone reports a write that "failed", that is expected: it was refused, nothing was saved, and it goes on paper.

## Afterwards

1. **End read-only mode:** Admin → Downtime → **End read-only mode** (`POST /api/downtime/end`). If you set
   `READ_ONLY_MODE`, remove it and redeploy first. Staff are notified to back-enter.
2. **Unplanned outage nobody switched read-only for?** Record it so its paper work can be entered: Admin → Downtime →
   **Record an unplanned outage** with the start and end times (`POST /api/downtime/periods`). It must be over, and must
   not overlap a recorded period.
3. **Back-enter the paper work**, unit by unit, from the printouts:
   - Tasks: on the workflow screen, each open task shows **Done on paper at**; enter the time from the form and press
     **Enter** (`PUT /api/tasks/{id}/backfill {"completed_at": "..."}`). The same rules as live updates apply: the
     assigned role (or named person) enters its own tasks, and a physician or admin entering another role's task is
     asked for a reason, stored on the task. Prefer the person who did the work entering it.
   - Discharges: once the tasks are in and the summary is approved, set **Discharged on paper during a downtime at**
     and press **Enter Paper Discharge** (`PUT /api/workflows/{id}/complete {"completed_at": "..."}`).
   - The time is your browser's local time, sent as UTC. It must be in the past, after the task or workflow was
     created, and inside the downtime period; anything else is refused with the reason.
4. **Check:** reprint the census; the unit's open tasks should match what is still undone on paper. Then shred the
   printouts.

## What is recorded

- `START_DOWNTIME`, `END_DOWNTIME` and `RECORD_OUTAGE` audit entries, naming the downtime id.
- Each back-entered task: `BACKFILL_TASK_STATUS`, with the time it was done and the downtime id. The task keeps the
  real time in `completed_at`, and `backfilled_at` says when it was entered (shown as "on paper" in the UI).
- A back-entered discharge: `COMPLETE_DISCHARGE` with "backfilled: discharged at ... (downtime ...)", and
  `workflows.backfilled_at`.
- Each census load or download: one `PHI_READ` entry with the unit and the number of patients.
- Operational metrics use the real times, so a downtime does not distort task SLA or discharge duration. The time a
  workflow spent waiting for approval is counted up to the moment it was entered, not the paper time.

## Limits

- The census is only as current as its printout. Print it again at each shift change during a long downtime.
- Back-entry records the person entering the work as `completed_by`; when someone else did it, the paper form is
  the source, so keep it until the entries are checked.
- New admissions done on paper reach DischargeFlow only through the ADT feed (or by entering the patient afterwards);
  there is no bulk import of paper admissions.
- Summaries can't be generated or approved during read-only mode. A discharge that can't wait is done on paper and
  its summary written up afterwards.
