# API reference

Every route is under `/api`. Requests carry `Authorization: Bearer <access token>` unless the route is public or uses
an integration API key. FastAPI also serves interactive OpenAPI docs at `/docs` on a locally running backend
(http://localhost:8000/docs); on Vercel only `/api/*` reaches the backend.

**Access** column: *public* needs no credentials; *signed in* needs a valid session for an approved account; a
permission name (`workflow:manage`) means the user's role must hold it. The API enforces these whatever the UI shows.
Accounts with the role `pending` (signed in, not yet approved) get `403 Account pending approval` from every route
except `/auth/me`, `/auth/refresh` and `/auth/logout`.

**Read-only (downtime) mode.** While an admin has switched it on (`POST /downtime/start`), or `READ_ONLY_MODE=true` is
set, every POST, PUT, PATCH and DELETE answers `503` with header `X-Read-Only: true` and a `detail` telling staff to use
the paper downtime forms, before the route runs or its body is validated. Exempt: `/auth/*` (sign-in, refresh,
sign-out) and `POST /downtime/end`. Reads keep working, including their `PHI_READ` audit entries. HL7 ADT messages get
the 503 too, so the interface engine queues and resends them. Procedure: [runbooks/downtime.md](runbooks/downtime.md).

## Roles and permissions

Stored in the `role_permissions` table (seeded by migrations from `identity/permissions.py`). Admin holds every
permission. `interface` is the role of integration clients (API keys), not a role a person can be given. `pending`
holds no permissions: it is the role of new sign-ins until an admin approves them, and an admin can set it again to
suspend someone.

| Permission | Held by |
|---|---|
| `patient:write` | physician, nurse |
| `patient:erase` | admin |
| `workflow:manage` | physician |
| `summary:manage` | physician |
| `task:create` | physician, nurse |
| `task:override` | physician (change the status of another role's task, with a reason) |
| `task:assign` | physician (assign a task to a named user; members of the task's role may claim it themselves) |
| `user:manage` | admin |
| `audit:read` | admin |
| `audit:read_patient` | physician |
| `insights:read` | admin |
| `adt:ingest` | interface |
| `claim:manage` | nurse (create insurance claims, change their status) |
| `patient:read_all` | interface (never unit-scoped; admin holds it too) |
| `downtime:manage` | admin (read-only mode on and off, record outages) |
| `escalation:run` | interface (a hospital scheduler running the escalation of overdue tasks) |
| `plan:manage` | physician, nurse (set and review a patient's discharge plan) |
| `template:manage` | admin (create and edit workflow templates) |
| `capacity:read` | physician, nurse (the capacity board) |
| `bed:manage` | nurse, interface (move a bed through dirty, cleaning, available; the EVS system uses an API key) |
| `instructions:manage` | physician, nurse (draft, edit, approve and print patient instructions) |

### Unit-scoped access

With `UNIT_SCOPED_ACCESS=true` (default `false`), a user whose role lacks `patient:read_all` sees only patients on
their own units, patients with no unit, and patients they opened with break-glass in the last 8 hours. Everything
about a hidden patient is hidden too: workflows, tasks, summaries and claims, including in lists, FHIR and the
assistant. PostgreSQL RLS enforces this (SQLite, for local development, does not). `GET /patients/{id}` for a hidden
patient answers `403 {"detail": ..., "break_glass": true}`; other routes answer as if the patient did not exist.
Notifications about a patient on a unit go only to members of that unit.

## Authentication

| Method | Path | Access | Notes |
|---|---|---|---|
| POST | `/auth/session` | public | Exchanges the Emergent OAuth `session_id` for an access token; sets the refresh cookie. Known (or invited) users sign in with their role. Unknown accounts become `pending` if `SELF_REGISTRATION` is on and their domain is in `ALLOWED_EMAIL_DOMAINS` (any domain when unset), otherwise 403. Admins are notified; audited (`REQUEST_ACCESS`) |
| GET | `/auth/config` | public | Sign-in methods offered: `{"oidc": {"issuer", "client_id"}}` when hospital SSO is configured, else `{"oidc": null}` |
| POST | `/auth/oidc` | public | Hospital SSO (ADR-002 item 2, seam): body `{id_token, nonce}`. Verifies the ID token against the provider's JWKS (issuer, audience, expiry, nonce, `token_use`, `email_verified`, `amr` when `OIDC_REQUIRE_MFA`), then signs in like `/auth/session`. 401 naming the failed check; 404 unless `OIDC_ISSUER` and `OIDC_CLIENT_ID` are set |
| POST | `/auth/refresh` | refresh cookie | Rotates the refresh cookie and returns a new 15-minute access token. 401 (and the session is revoked) if it wasn't refreshed for `SESSION_IDLE_MINUTES` |
| GET | `/auth/me` | signed in, including `pending` | The current user, role and tenant |
| POST | `/auth/logout` | refresh cookie | Revokes the session. Optional body `{"reason": "idle"}` when the browser locked itself for inactivity (stored as `idle_lock`) |
| POST | `/auth/dev-login` | development/test only | Enabled by `ENABLE_DEV_LOGIN`; used by Playwright |

## Users and integration clients

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/users` | `user:manage` | Each with `unit_ids` |
| POST | `/users` | `user:manage` | Invite: pre-creates a user by email with a role, which their first Google sign-in gets (regardless of the domain allow-list or `SELF_REGISTRATION`). 409 if the email exists; audited |
| PUT | `/users/{user_id}/units` | `user:manage` | Body `{unit_ids}`: replaces the user's unit memberships; applies within seconds; audited |
| GET | `/units` | signed in | Units (wards), by code |
| POST | `/units` | `user:manage` | Body `{code, name}`. The code (stored upper-case, unique) should match the ADT point of care, PV1-3.1; audited |
| PUT | `/users/{user_id}/role` | `user:manage` | Approve (`pending` → a role), change, or suspend (→ `pending`); applies within seconds; audited |
| GET | `/integrations/clients` | `user:manage` | API-key clients for hospital systems; never returns key material |
| POST | `/integrations/clients` | `user:manage` | Returns the API key once |
| DELETE | `/integrations/clients/{client_id}` | `user:manage` | Revokes the key |

## Patients

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/patients` | signed in | Optional `?status=`; one `PHI_READ` audit entry per request (not per row) |
| GET | `/patients/{patient_id}` | signed in | `PHI_READ` audited; 403 with `break_glass: true` when unit scope hides the patient |
| GET | `/patients/{patient_id}/encounters` | signed in | The patient's stays (encounters), newest first: `status` (`active` or `discharged`), `admitted_at`, `discharged_at`, `unit_id`, `room_number`, `visit_number`, `disposition`, `expected_discharge_date`, and `workflows` (id, status, start, completion). One `PHI_READ` entry per request; 403/404 as for the patient |
| POST | `/patients/{patient_id}/break-glass` | signed in (unit-scoped users) | Body `{reason}` (10–500 characters). Opens one patient outside your units for 8 hours. The reason is stored on the grant, not in the audit entry; audited (`BREAK_GLASS`) and admins are notified. 400 if you are not unit-scoped, 409 if you can already see the patient |
| GET | `/access-grants` | `audit:read` | Break-glass grants for review, newest first, with user name, reason and `active`; `?active_only=true` |
| POST | `/patients` | `patient:write` | Optional `unit_id` and `preferred_language` (BCP 47 code such as `es` or `zh-Hant`; 422 otherwise); opens the patient's first encounter from the admission fields; audited (`CREATE_PATIENT`) |
| PUT | `/patients/{patient_id}` | `patient:write` | Whitelisted fields only (including `preferred_language`); audited with the field names, never values. Room, unit and admission date are copied to the current encounter; `status: discharged` closes it, and setting a discharged patient back opens a new one |
| POST | `/patients/{patient_id}/consent` | signed in | GDPR consent; audited |
| DELETE | `/patients/{patient_id}/data` | `patient:erase` | Right to erasure: destroys the patient's data key and overwrites summaries, patient instructions and visit numbers; audited |

## Discharge workflows and tasks

| Method | Path | Access | Notes |
|---|---|---|---|
| POST | `/workflows/initiate/{patient_id}` | `workflow:manage` | Creates the tasks of the active template for the stay's `planned_disposition` (with prerequisites and due times from the expected discharge date), or the eight standard tasks when there is no plan or template. Returns `{workflow, tasks_created, template}` (`template` null for the standard tasks); audited with the template id and version |
| GET | `/workflows` | signed in | Optional `?status=` |
| GET | `/workflows/patient/{patient_id}` | signed in | The active workflow, or `null`; `PHI_READ` audited when one is returned |
| GET | `/workflows/{workflow_id}` | signed in | With tasks and summary (as `GET /ai/summary`), plus `summary_sign_by` and `summary_overdue`; `PHI_READ` audited. Each task adds `due` (its due time: `due_at` from the template, else the SLA target for its priority after creation), `overdue`, and `blocked_by` (prerequisites not yet completed, `{task_id, title}`); the workflow adds `plan` (the stay's discharge plan) |
| PUT | `/workflows/{workflow_id}/complete` | `workflow:manage` | Requires no open tasks and an approved summary; audited. Optional body `{completed_at}` back-enters a discharge done on paper: the time must be in the past, after the workflow started and inside a recorded downtime period; it becomes `completed_at`, `backfilled_at` records when it was entered, and the audit entry names the downtime |
| POST | `/tasks` | `task:create` | Attached to the active workflow if there is one; audited |
| GET | `/tasks` | signed in | |
| GET | `/tasks/my` | signed in | Open tasks assigned to the user or the user's role |
| PUT | `/tasks/{task_id}` | signed in | Body `{status?, assigned_to?, reason?, delay_code?, delay_note?}`. Status: the assignee (named user or assigned role); anyone else needs `task:override` and a `reason`, stored on the task as `override_reason`/`override_by`. `assigned_to`: needs `task:assign`, except claiming a task of your own role; the assignee must be an approved user. Starting or completing a task whose prerequisites (`depends_on`) are not all completed answers 409 `Waiting on: …`, for overrides too. Completing a task after its due time needs a `delay_code` (400 otherwise) unless one is on record; it is stored as a delay reason and audited as `RECORD_DELAY_REASON`. Each change is audited; moves the workflow between states |
| POST | `/tasks/{task_id}/delay-reason` | signed in | Body `{code, note?}`: why an open task is past its due time, so completing it needs no further reason. The assignee, or anyone holding `task:override`. 400 if the task is not open and late, 409 if a reason is already recorded |
| PUT | `/tasks/{task_id}/backfill` | signed in | Body `{completed_at, reason?, delay_code?, delay_note?}` (timezone required). Completes a task done on paper during a downtime at the time it was done: in the past, after the task was created, inside a recorded downtime period. Same ownership rules as `PUT /tasks/{task_id}` (another role's task needs `task:override` and a `reason`). Sets `backfilled_at`; audited as `BACKFILL_TASK_STATUS` naming the downtime. 400 if already completed |

## Discharge plans and workflow templates

The plan lives on the encounter (ADR-002 items 8-10). "Today" is the date in `HOSPITAL_TIMEZONE`. Delay codes:
`medical`, `social`, `placement`, `insurance`, `transport`, `pharmacy`, `documentation`, `other` (`other` needs a
note). Notes stay on the delay row (never in audit entries) and erasure redacts them. Dispositions: `home`,
`home_services`, `snf`, `rehab`, `hospice`, `ama`, `transfer`, `expired`.

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/discharge-plans/codes` | signed in | `{dispositions, delay_codes, roles}` for the plan and template screens |
| GET | `/discharge-plans/review` | signed in | Active stays grouped by unit (`unit_id`, `unit_code`, `unit_name`, `total`, `not_reviewed_today`, `plans`), not-reviewed first. Each plan: patient name and room, `expected_discharge_date`, `planned_disposition`, `plan_reviewed_at`/`_by`/`_by_name`, `reviewed_today`, `expected_date_passed`, `workflow_status`. `?unit_id=` (or `unassigned`), `?pending_only=true`. Unit scope applies; one `PHI_READ` entry per request |
| GET | `/encounters/{encounter_id}/plan` | signed in | The plan with its review state and `delays`; `PHI_READ` audited |
| PUT | `/encounters/{encounter_id}/plan` | `plan:manage` | Body `{expected_discharge_date?, planned_disposition?, delay_code?, delay_note?}`; fields left out stay, null clears. Moving the date later needs a `delay_code` (recorded as an `expected_date_slip` with `slip_days`). Moves the due times of the open template tasks of the active workflow. Counts as today's review. 400 for a discharged stay. Audited as `UPDATE_DISCHARGE_PLAN` with field names and the delay code only |
| POST | `/encounters/{encounter_id}/plan/review` | `plan:manage` | Optional body `{delay_code?, delay_note?}`. Records today's review (who and when). When the expected date has passed, the first review of the day needs a `delay_code`. Audited as `REVIEW_DISCHARGE_PLAN` |
| GET | `/workflow-templates` | signed in | Every template of the tenant: `disposition`, `name`, `version`, `active`, `tasks` (`key`, `title`, `description`, `category`, `assigned_role`, `priority`, `due_offset_hours` from 00:00 on the expected date, `depends_on` keys) |
| POST | `/workflow-templates` | `template:manage` | Body as a template above (without id and version). 409 if the disposition already has one; 422 for unknown roles, dispositions or prerequisites, duplicate keys or a dependency cycle. Audited |
| PUT | `/workflow-templates/{template_id}` | `template:manage` | Replaces name, tasks and `active`; the version goes up. Running workflows keep their tasks. The disposition can't change (400). Audited |

## AI

| Method | Path | Access | Notes |
|---|---|---|---|
| POST | `/ai/generate-summary` | `summary:manage` | Structured, de-identified generation; 503 when `LLM_PROVIDER=none`. When this session launched from the EHR for the patient, the EHR's active medications, problems and allergies are added to the facts (de-identified) and audited as a `PHI_READ` ("EHR clinical context (summary)"); a discharge medication matching a recorded allergy adds a grounding warning of type `allergy` |
| PUT | `/ai/approve-summary/{summary_id}` | `summary:manage` | Signs the summary: accepts the physician's edited text, stores the diff, and freezes the text as version 1 (`version_id`, `content_sha256` in the response). 400 if already signed; signed text never changes again |
| GET | `/ai/summary/{workflow_id}` | signed in | `PHI_READ` audited when a summary is returned. Adds `signed`, `versions` (metadata), `signed_version` (latest signed text, `content_sha256`, `hash_verified`), its `addenda`, `sign_by` and `signed_late` |
| POST | `/ai/summaries/{summary_id}/addenda` | `summary:manage` | Body `{content}` (1–20000 characters). Appends text to the latest signed version, signed by the author; 201 with the addendum. 400 if the summary is unsigned or the patient was erased. Audited as `SUMMARY_ADDENDUM_SIGNED` (addendum and version ids only). Allowed after discharge |
| GET | `/ai/summaries/overdue` | `summary:manage` | Discharges whose summary is missing or unsigned past the sign-by deadline (`SUMMARY_SIGN_HOURS` after discharge, or after the expected discharge time while the patient is still here). Ids and times only |
| POST | `/ai/summaries/overdue/notify` | `summary:manage` | Notifies physicians (on the patient's unit) once per overdue discharge; what `python -m app.cli summary-deadlines` runs for every tenant |
| POST | `/ai/patient-instructions` | `instructions:manage` | Body `{workflow_id, language?}`. Drafts patient-facing instructions from the **signed** summary only (latest signed version plus its addenda), de-identified, in `language` (BCP 47; default the patient's `preferred_language`, else `en`). Returns the draft with `reading_grade` (Flesch-Kincaid, English only, else null), `target_grade`, `stale`, and `generation` (provenance and `grounding_warnings`: `medication`, `diagnosis`, `omission`, `readability`). 404 without a summary, 409 if it is unsigned or the instructions in that language are already approved; an unapproved draft in the same language is replaced. 502 (provenance and audit kept) after one retry on invalid output; 503 when `LLM_PROVIDER=none`. Audited as `AI_INSTRUCTIONS_GENERATED` / `AI_INSTRUCTIONS_FAILED` (ids, language, counts) |
| GET | `/ai/patient-instructions?workflow_id=` | signed in | The workflow's instructions, one per language, with provenance. `stale` is true when an addendum was signed after the draft. One `PHI_READ` entry per request |
| PUT | `/ai/patient-instructions/{instructions_id}/approve` | `instructions:manage` | Optional body `{content}` (the reviewer's edited text). Stores `original_content`, `edit_diff`, `edit_similarity` and `approved_reading_grade`; approved text never changes. 409 if already approved. Audited as `PATIENT_INSTRUCTIONS_APPROVED` |
| GET | `/ai/patient-instructions/{instructions_id}/print` | `instructions:manage` | The approved handout with the patient's name, language, approver and `stale`. 409 for a draft, 410 after erasure. `PHI_READ` audited |
| GET | `/ai/generations` | `summary:manage` | Provenance records (no prompt or output text) |
| GET | `/ai/metrics` | `summary:manage` | Approval rate and edit similarity per prompt version |
| POST | `/chatbot/message` | signed in | Tool-calling assistant; write tools become pending actions |
| POST | `/chatbot/actions/{action_id}/confirm` | signed in | Runs a proposed write; same user, single use, 10-minute expiry, permission re-checked |
| POST | `/chatbot/actions/{action_id}/cancel` | signed in | |

## Analytics and notifications

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/analytics/dashboard` | signed in | KPIs |
| GET | `/analytics/tasks-by-role` | signed in | |
| GET | `/analytics/aggregates` | `insights:read` | Aggregate-only metrics sent to the insights model |
| GET | `/analytics/operations` | `insights:read` | Throughput, approval wait, task SLA (a task is late after its template due time, else its priority's SLA target), summary signing against the sign-by policy, `stays`: length of stay of encounters discharged in the window and readmissions within 30 days, and `delays`: delay reasons recorded in the window, by kind (`expected_date_slip`, `task_sla_breach`) and code, with the average slip in days only for groups of 3 or more (`?days=30`) |
| GET | `/analytics/outcomes` | `insights:read` | Outcomes of stays discharged in the window (`?days=90`, 1 to 365), `overall` and `by_unit` (unit code, or `unassigned`): `discharges`, `length_of_stay_days`, `vs_expected_date` (discharge day minus the final expected date, plus `on_or_before`), `expected_date_accuracy` (exact on the final and on the first expected date, absolute error in days, how many first estimates changed), `before_noon` (hospital time) and `readmitted_within_30_days` (another admission within 30 days, counted only for discharges at least 30 days ago; the rest are `follow_up_pending`). `delay_reasons`: a Pareto of codes (`n`, `share`, `cumulative_share`), by kind and by unit. Counts and durations only; a group under 3 gets its count but no averages or rates. Attribution is by unit only (no service line in the data model) |
| POST | `/analytics/insights` | `insights:read` | AI explanation of the aggregates |
| GET | `/notifications` | signed in | |
| GET | `/notifications/stream` | signed in | Server-Sent Events (`ready`, `notification` with the id only, `end`); closes after `NOTIFICATION_STREAM_SECONDS` and the client reconnects. A `ready` mid-stream means "refetch the list". `NOTIFICATION_TRANSPORT=listen` delivers over Postgres LISTEN/NOTIFY, `poll` (default, Vercel) polls every 5 s |
| PUT | `/notifications/{notification_id}/read` | signed in | |
| PUT | `/notifications/read-all` | signed in | |

## Downtime

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/downtime/status` | signed in | `{read_only, forced, period}`; `forced` means `READ_ONLY_MODE` is set. The UI polls it every 30 s |
| POST | `/downtime/start` | `downtime:manage` | Body `{reason}` (3–300 characters, shown to every user, so never patient details). Opens a `read_only` period for the tenant; staff are notified; audited (`START_DOWNTIME`). Stored in the database, so every instance refuses writes on its next request |
| POST | `/downtime/end` | `downtime:manage` | Works while read-only. Closes the open period; staff are notified to back-enter paper work; audited (`END_DOWNTIME`). 409 if none is open. Does not lift `READ_ONLY_MODE` |
| POST | `/downtime/periods` | `downtime:manage` | Body `{started_at, ended_at, reason}`: records an unplanned outage after it is over, so work done on paper during it can be back-entered. 400 if it ends before it starts or in the future, 409 if it overlaps a recorded period; audited (`RECORD_OUTAGE`) |
| GET | `/downtime/periods` | signed in | The latest 50 periods (`read_only` or `outage`), newest first |
| GET | `/census` | signed in | Discharge census: patients not yet discharged (erased ones left out), by unit and room, with the current encounter (id, visit number, admission, room), discharge status, estimated discharge, whether the summary is approved and the open tasks. `?unit_id=` (or `unassigned`; 404 for an unknown unit); `?format=csv` downloads it (spreadsheet formulas neutralised). Unit scope applies as for `/patients`. One `PHI_READ` entry per request with the scope and count |

## Capacity and beds

Capacity board and the bed-to-cleaning handoff (ADR-002 item 17). A bed is a unit plus a room (the location strings
encounters carry). When a stay ends in it (discharge completed in the UI, a manual status change to discharged, an
HL7 A03 with or without a workflow) or a patient is transferred out of it (A02), the bed gets a turnover in state
`dirty`; at most one open turnover per bed. A13 (discharge cancelled) cancels a `dirty` turnover from that discharge.
Responses never include the encounter or patient. Unit scope applies: Postgres RLS on `bed_turnovers` by its own
`unit_id`, and the board lists only the user's units.

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/capacity/board` | `capacity:read` | `?date=YYYY-MM-DD` (hospital day, default today). Per unit (`units`, unassigned last) and in `totals`: `in_house`, `expected` (plan's expected date is that day), `expected_by_hour` (hour of the active workflow's `estimated_discharge_time`, hospital time) and `unscheduled` (expected with no time that day), `expected_tomorrow`, `past_expected_date` (today only), `no_expected_date`, `discharged`, `discharged_by_hour`, `discharged_before_noon`, `before_noon_7d` (`n`, `before_noon`, `rate`), bed counts (`beds_dirty`, `beds_cleaning`, `beds_available` in the last 12 h) and `turnaround_minutes_7d` (median dirty to available). `beds`: open turnovers and ones made available in the last 12 h, with `minutes_out_of_service`. Operational: small counts are shown as they are |
| GET | `/beds` | `bed:manage` (signed in or `X-API-Key`) | Turnovers oldest first: open ones, or `?status=dirty\|cleaning\|available\|cancelled` (≤500) |
| POST | `/beds` | `bed:manage` (signed in or `X-API-Key`) | `{unit_id, room_number}`: a bed that needs cleaning without a discharge the system saw. Returns the open turnover if there is one. 400 unknown unit, 403 a unit the user isn't on. Audited `BED_DIRTY` |
| PUT | `/beds/{turnover_id}` | `bed:manage` (signed in or `X-API-Key`) | `{status: "cleaning" \| "available"}`: dirty to cleaning, cleaning to available, or dirty to available (no start time recorded). 409 for any other move. Records the time and who. Audited `BED_STATUS` with the unit and the move, never the room |

## Escalations

Escalation of overdue tasks (ADR-002 item 12). A task breaches when it is open longer than its SLA target (high 4 h,
medium 12 h, low 24 h). Level 1, at the breach, notifies the assignee (the named user, or the assigned role on the
patient's unit); level 2, a further 1 h (high) or 4 h (medium) later and never sooner than that after level 1,
notifies the supervising role (nurse and pharmacist tasks: physician; physician tasks: admin). Low priority has level 1
only. High priority is paged at both levels and medium at level 2, through `ESCALATION_PAGER` (`none` default, or
`webhook`). Each step is audited (`ESCALATE_TASK`, and `PAGE_ESCALATION` per page attempt). Notifications and pages
carry ids, category, priority, unit code, minutes overdue and a link, never the task title or patient data.

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/escalations/run` | `Authorization: Bearer <CRON_SECRET>` | Vercel Cron's call (cron jobs send GET; schedule in `vercel.json`, daily because the Hobby plan allows no more, so a scheduler calling `POST` every few minutes is the real cadence). Runs every tenant, each in its own transaction. 401 for anything else, and always while `CRON_SECRET` is unset |
| POST | `/escalations/run` | cron secret, or `escalation:run` (API key or signed in) | Idempotent: takes only the steps that are due and missing (at most one new step per task per run, 100 per run, 20 pages per run), resolves escalations of completed tasks, retries failed pages up to 3 times. The cron secret runs every tenant; a key or user runs their own. Returns `{tenants: [{tenant_id, escalated, resolved, paged, page_failures, skipped}]}`; `skipped` is `read_only` (a downtime: tasks can't be completed, so nothing escalates) or `already_running` (another run holds the tenant's lock, Postgres). Refused with 503 in read-only mode like any write, except with the cron secret, where the run skips the tenant instead |
| GET | `/escalations` | `insights:read` | Active escalations (task still open), newest first, `?limit=` (≤500): ids, level, priority, category, unit code, who was told and how many (`recipients`; 0 means nobody holds that role on the unit), page status (`not_required`, `disabled`, `pending`, `sent`, `failed`, `cancelled`) and a link; plus the policy, the supervisor roles, the pager and whether a schedule is configured |

**Webhook pages.** `POST ESCALATION_WEBHOOK_URL` with JSON `{type: "task.escalated", escalation_id, idempotency_key,
tenant_id, task_id, level, priority, category, unit, target_role, overdue_minutes, link}`, headers `Idempotency-Key`
(stable per tenant, task and level; drop repeats) and `X-DischargeFlow-Signature: t=<unix seconds>,v1=<hex>`, where
`v1` is HMAC-SHA256 of `<t>.<raw body>` with `ESCALATION_WEBHOOK_SECRET`. Verify the signature, refuse old `t`, and
answer 2xx to accept; anything else (or a 5 s timeout) counts as a failed attempt. `link` is absolute when
`APP_BASE_URL` is set.

## Insurance

| Method | Path | Access | Notes |
|---|---|---|---|
| POST | `/insurance/claims` | `claim:manage` | Audited |
| GET | `/insurance/claims` | signed in | |
| GET | `/insurance/claims/{claim_id}` | signed in | |
| PUT | `/insurance/claims/{claim_id}/status` | `claim:manage` | pending, submitted, accepted, denied, paid; audited |
| GET | `/insurance/reports` | signed in | |

## Audit

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/audit/logs` | `audit:read` | |
| GET | `/audit/verify` | `audit:read` | Recomputes the hash chain; reports the first broken entry |
| GET | `/audit/patients/{patient_id}` | `audit:read_patient` | The latest 100 audit entries about one patient, including who read their record (`PHI_READ`) |

## Interoperability

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/fhir/R4/metadata` | public | CapabilityStatement |
| GET | `/fhir/R4/Patient`, `/fhir/R4/Patient/{id}` | signed in | Search returns a Bundle |
| GET | `/fhir/R4/Encounter`, `/fhir/R4/Encounter/{id}` | signed in | Inpatient stays (encounters), with the visit number, period, location and discharge disposition; `?patient=`. Task and DocumentReference point to the encounter of their workflow, and their `?encounter=` search uses it |
| GET | `/fhir/R4/Task`, `/fhir/R4/Task/{id}` | signed in | |
| GET | `/fhir/R4/DocumentReference`, `/fhir/R4/DocumentReference/{id}` | signed in | Signed discharge summaries: the latest signed version, then one attachment per addendum; `docStatus` is `amended` when there are addenda |
| POST | `/hl7/adt` | integration key with `adt:ingest` | See below |
| GET | `/hl7/parked` | `user:manage` | Parked out-of-order ADT messages, newest first (at most 500); `?status=parked\|replayed\|expired\|discarded`. Ids, event, control id, times and replay outcome only, never the message. Expires overdue ones first |
| POST | `/hl7/parked/{parked_id}/discard` | `user:manage` | Drops a parked message unapplied (its ciphertext and key are deleted); 400 unless still parked |

### HL7 v2 ADT

`POST /api/hl7/adt` with header `X-API-Key: <tenant>.<client_id>.<secret>` and one ER7 message as the body. The
response is the ACK (`x-application/hl7-v2+er7`) with HTTP 200 for every outcome HL7 can express:

| ACK | When |
|---|---|
| AA | **A01** admitted a new MRN or readmitted a discharged patient (each opens a new encounter). **A02** moved the patient to the unit and room/bed in PV1-3. **A03** completed the discharge workflow; or, when the workflow can't complete yet (open tasks, unapproved summary), recorded the discharge anyway: the encounter closes at the departure time, the patient is discharged and the workflow becomes `externally_discharged` with its tasks still open (text "Discharge recorded; workflow left open (reason)"); or closed the stay when no workflow had been started; or the patient was already discharged. **A08** updated the whitelisted fields it carries (name, date of birth, attending, diagnosis; the text lists field names). **A11** cancelled the current encounter (status `cancelled`). **A13** reopened the last stay, and an `externally_discharged` workflow went back to in progress. **A40** merged the MRG-1 record into the PID-3 one, or gave the MRG-1 record the surviving MRN when only it was known, or found nothing to merge. **Any event but A01/A40 for an MRN not seen yet** was parked (text starts "Parked:"). An A01 whose MRN had parked messages adds "; N parked message(s) applied" |
| AE | MRN already admitted or PV1-19 visit number already admitted (205), required field missing (101) or invalid (102), PV1-19 names another visit than the patient's encounter (207), A02 for a patient not admitted (207), A11 for an admission with a discharge workflow (207; physicians and admins are notified), A11 with no admission in progress (207), A40 without MRG (100) or naming the survivor itself (102), A40 that can't merge automatically, e.g. both records have a stay in progress or one was erased (207; admins are notified) |
| AR | Not ADT (200), an event other than A01, A02, A03, A08, A11, A13, A40 (201), MSH-11 not `P` (202), unparseable message (100), no control id (101) |

HTTP errors are reserved for problems below HL7: 401 (missing, invalid or revoked key), 403 (no `adt:ingest`), 413
(over 256 KB), 409 (the same control id is being processed concurrently; retry). A resent message with the same
MSH-10 control id gets its original ACK and is not applied again (a parked message is not parked twice).

Patients are matched on PID-3 (the repetition typed `MR`, else the first) plus its assigning authority. Fields read:
PID-5 name, PID-7 date of birth, PV1-3 location, PV1-7 attending, PV1-19 visit number (stored encrypted on the
encounter), PV1-36 discharge disposition (A03), PV1-44 admit time (else EVN-2 or MSH-7), PV1-45 discharge time (A03;
else EVN-6, EVN-2, MSH-7; a time without offset is taken as UTC), DG1-3 or PV2-3 diagnosis, MRG-1 prior MRN (A40).
Missing optional fields are stored as "Not provided by ADT".

**Externally discharged workflows.** `documentation_due_at` is `departed_at` plus `ADT_DOCUMENTATION_HOURS` (default
24). Physicians and the unit's nurses are notified. `PUT /workflows/{id}/complete` completes it once the tasks are done
and the summary is approved, with `completed_at` = the departure time (a `completed_at` in the body is refused).

**Parking.** A message parked for an unknown MRN is stored as AES-256-GCM ciphertext under its own data key, with the
blind index of the MRN it waits for. It is replayed, in arrival order and in the same transaction, when an A01 (or an
A40 re-key) brings that MRN in; the replay is audited as `HL7_ADT_REPLAYED`. After `HL7_PARK_HOURS` (default 72) it
expires unapplied and admins are notified. Replayed, expired and discarded messages keep only their metadata.

## SMART on FHIR EHR launch and clinical context

ADR-002 items 14 and 15. The EHR opens `{app}/launch?iss=…&launch=…`; the browser runs the authorization-code flow
with PKCE (S256) and the server does the token exchange, so the EHR's tokens never reach the browser. Scopes:
`launch openid fhirUser patient/*.read`. Only allow-listed FHIR servers may launch; nothing is fetched from any other.

| Method | Path | Access | Notes |
|---|---|---|---|
| POST | `/smart/launch` | public | Body `{iss, launch, code_challenge}` (S256, base64url). 403 unless `iss` is a registered, enabled issuer; discovers the endpoints from `{iss}/.well-known/smart-configuration` (502 if unreadable, without an authorization or token endpoint, or without S256). Returns `{authorize_url, state}`; the pending launch lasts 10 minutes. Allowed in read-only mode |
| POST | `/smart/token` | public | Body `{state, code, code_verifier}`. Single use per state. Checks the verifier against the stored challenge, exchanges the code, verifies the id_token (signature by a key from the server's `jwks_uri`, `iss`, `aud` = client id, expiry, nonce when echoed, a `fhirUser` claim). The EHR user maps to a DischargeFlow user through an admin link, else by email (the id_token's, else the `fhirUser` resource's `telecom`), with the Google login's rules (unknown → `pending`; 403 when registration refuses it or there is no email). Returns the `/auth/session` response (access token, `df_refresh` cookie) plus `launch: {launch_id, patient_id, encounter_id, workflow_id, matched, note}`: the EHR patient's MRN identifier (`mrn_system`, else the one typed `MR`) is looked up through the MRN blind index with `mrn_authority`, and the encounter by the visit number in `visit_system`, else the active stay. No match still signs in (`matched: false` and the reason). Audited as `SMART_LAUNCH` (patient id, launch and issuer ids); failures as `SMART_LAUNCH_FAILED` (reason only). 400 for an unknown, used or expired state or a bad verifier, 401 for a refused code or id_token, 502 when the EHR misbehaves. Allowed in read-only mode |
| GET | `/smart/clinical-context/{patient_id}` | signed in | The EHR's active `MedicationRequest`s (`name`, `dosage`, `status`), `Condition`s (`name`, `clinical_status`, `category`; refuted, entered-in-error, inactive and resolved left out) and `AllergyIntolerance`s (`substance`, `criticality`, `reactions`), read live with the token of **this session's** launch for this patient: `{available: true, source, fetched_at, medications, problems, allergies, errors}` (`errors` names any resource type the EHR did not return). Otherwise `{available: false, reason: no_launch \| expired \| unavailable, detail}`. Cached 60 s in the server process, never stored. One `PHI_READ` entry per request that returns data. 403/404 as for the patient |
| GET | `/smart/issuers` | `user:manage` | The allow-list |
| POST | `/smart/issuers` | `user:manage` | Body `{iss, name, client_id, redirect_uri, mrn_system?, mrn_authority?, visit_system?, enabled?}`: an EHR's FHIR base URL and the public client it registered for DischargeFlow (redirect URI `{app}/launch`). https only (http to localhost outside production). 409 if the `iss` is registered; audited |
| PUT | `/smart/issuers/{issuer_id}` | `user:manage` | Same body. Disabling ends the issuer's pending and active launches (their EHR tokens are dropped); audited with the changed field names |
| GET | `/smart/links` | `user:manage` | EHR users (`fhir_user`) linked to DischargeFlow users |
| POST | `/smart/links` | `user:manage` | Body `{issuer_id, fhir_user, user_id}`: for EHR accounts without an email to match. 409 if linked; audited |
| DELETE | `/smart/links/{link_id}` | `user:manage` | Audited |

**EHR token.** Kept server-side only, as AES-256-GCM ciphertext under the launch's own data key, with the EHR patient
and encounter ids. Its lifetime is the EHR's `expires_in`, capped at one hour; no refresh token is requested. It is
used only by the DischargeFlow session that made the launch, so after an idle lock or another person's sign-in the
lists are gone until the next launch. When it expires, the EHR refuses it (401) or the issuer is disabled, the
ciphertext and the key are dropped. FHIR requests have an 8-second timeout and go only to the launch's `iss`.

## Health

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/health` | public | Database reachability and API version only |
