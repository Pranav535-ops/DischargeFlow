# API reference

Every route is under `/api`. Requests carry `Authorization: Bearer <access token>` unless the route is public or uses
an integration API key. FastAPI also serves interactive OpenAPI docs at `/docs` on a locally running backend
(http://localhost:8000/docs); on Vercel only `/api/*` reaches the backend.

**Access** column: *public* needs no credentials; *signed in* needs a valid session for an approved account; a
permission name (`workflow:manage`) means the user's role must hold it. The API enforces these whatever the UI shows.
Accounts with the role `pending` (signed in, not yet approved) get `403 Account pending approval` from every route
except `/auth/me`, `/auth/refresh` and `/auth/logout`.

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

## Authentication

| Method | Path | Access | Notes |
|---|---|---|---|
| POST | `/auth/session` | public | Exchanges the Emergent OAuth `session_id` for an access token; sets the refresh cookie. Known (or invited) users sign in with their role. Unknown accounts become `pending` if `SELF_REGISTRATION` is on and their domain is in `ALLOWED_EMAIL_DOMAINS` (any domain when unset), otherwise 403. Admins are notified; audited (`REQUEST_ACCESS`) |
| POST | `/auth/refresh` | refresh cookie | Rotates the refresh cookie and returns a new 15-minute access token. 401 (and the session is revoked) if it wasn't refreshed for `SESSION_IDLE_MINUTES` |
| GET | `/auth/me` | signed in, including `pending` | The current user, role and tenant |
| POST | `/auth/logout` | refresh cookie | Revokes the session. Optional body `{"reason": "idle"}` when the browser locked itself for inactivity (stored as `idle_lock`) |
| POST | `/auth/dev-login` | development/test only | Enabled by `ENABLE_DEV_LOGIN`; used by Playwright |

## Users and integration clients

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/users` | `user:manage` | |
| POST | `/users` | `user:manage` | Invite: pre-creates a user by email with a role, which their first Google sign-in gets (regardless of the domain allow-list or `SELF_REGISTRATION`). 409 if the email exists; audited |
| PUT | `/users/{user_id}/role` | `user:manage` | Approve (`pending` → a role), change, or suspend (→ `pending`); applies within seconds; audited |
| GET | `/integrations/clients` | `user:manage` | API-key clients for hospital systems; never returns key material |
| POST | `/integrations/clients` | `user:manage` | Returns the API key once |
| DELETE | `/integrations/clients/{client_id}` | `user:manage` | Revokes the key |

## Patients

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/patients` | signed in | Optional `?status=`; one `PHI_READ` audit entry per request (not per row) |
| GET | `/patients/{patient_id}` | signed in | `PHI_READ` audited |
| POST | `/patients` | `patient:write` | Audited (`CREATE_PATIENT`) |
| PUT | `/patients/{patient_id}` | `patient:write` | Whitelisted fields only; audited with the field names, never values |
| POST | `/patients/{patient_id}/consent` | signed in | GDPR consent; audited |
| DELETE | `/patients/{patient_id}/data` | `patient:erase` | Right to erasure: destroys the patient's data key; audited |

## Discharge workflows and tasks

| Method | Path | Access | Notes |
|---|---|---|---|
| POST | `/workflows/initiate/{patient_id}` | `workflow:manage` | Creates the eight standard tasks; audited |
| GET | `/workflows` | signed in | Optional `?status=` |
| GET | `/workflows/patient/{patient_id}` | signed in | The active workflow, or `null`; `PHI_READ` audited when one is returned |
| GET | `/workflows/{workflow_id}` | signed in | With tasks and summary; `PHI_READ` audited |
| PUT | `/workflows/{workflow_id}/complete` | `workflow:manage` | Requires no open tasks and an approved summary; audited |
| POST | `/tasks` | `task:create` | Attached to the active workflow if there is one; audited |
| GET | `/tasks` | signed in | |
| GET | `/tasks/my` | signed in | Open tasks assigned to the user or the user's role |
| PUT | `/tasks/{task_id}` | signed in | Body `{status?, assigned_to?, reason?}`. Status: the assignee (named user or assigned role); anyone else needs `task:override` and a `reason`, stored on the task as `override_reason`/`override_by`. `assigned_to`: needs `task:assign`, except claiming a task of your own role; the assignee must be an approved user. Each change is audited; moves the workflow between states |

## AI

| Method | Path | Access | Notes |
|---|---|---|---|
| POST | `/ai/generate-summary` | `summary:manage` | Structured, de-identified generation; 503 when `LLM_PROVIDER=none` |
| PUT | `/ai/approve-summary/{summary_id}` | `summary:manage` | Accepts the physician's edited text; stores the diff |
| GET | `/ai/summary/{workflow_id}` | signed in | `PHI_READ` audited when a summary is returned |
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
| GET | `/analytics/operations` | `insights:read` | Throughput, approval wait, task SLA (`?days=30`) |
| POST | `/analytics/insights` | `insights:read` | AI explanation of the aggregates |
| GET | `/notifications` | signed in | |
| GET | `/notifications/stream` | signed in | Server-Sent Events; closes after `NOTIFICATION_STREAM_SECONDS` and the client reconnects |
| PUT | `/notifications/{notification_id}/read` | signed in | |
| PUT | `/notifications/read-all` | signed in | |

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
| GET | `/fhir/R4/Encounter`, `/fhir/R4/Encounter/{id}` | signed in | Discharge workflows |
| GET | `/fhir/R4/Task`, `/fhir/R4/Task/{id}` | signed in | |
| GET | `/fhir/R4/DocumentReference`, `/fhir/R4/DocumentReference/{id}` | signed in | Approved discharge summaries |
| POST | `/hl7/adt` | integration key with `adt:ingest` | See below |

### HL7 v2 ADT

`POST /api/hl7/adt` with header `X-API-Key: <tenant>.<client_id>.<secret>` and one ER7 message as the body. The
response is the ACK (`x-application/hl7-v2+er7`) with HTTP 200 for every outcome HL7 can express:

| ACK | When |
|---|---|
| AA | A01 admitted a new MRN or readmitted a discharged patient; A03 completed the discharge, or the patient was already discharged |
| AE | Unknown MRN (204), MRN already admitted (205), required field missing (101) or invalid (102), or the discharge workflow can't complete (207, with the reason; physicians are notified) |
| AR | Not ADT (200), an event other than A01/A03 (201), MSH-11 not `P` (202), unparseable message (100), no control id (101) |

HTTP errors are reserved for problems below HL7: 401 (missing, invalid or revoked key), 403 (no `adt:ingest`), 413
(over 256 KB), 409 (the same control id is being processed concurrently; retry). A resent message with the same
MSH-10 control id gets its original ACK and is not applied again.

Patients are matched on PID-3 (the repetition typed `MR`, else the first) plus its assigning authority. Fields read:
PID-5 name, PID-7 date of birth, PV1-3 location, PV1-7 attending, PV1-44 admit time (else EVN-2 or MSH-7), DG1-3 or
PV2-3 diagnosis. Missing optional fields are stored as "Not provided by ADT".

## Health

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/health` | public | Database reachability and API version only |
