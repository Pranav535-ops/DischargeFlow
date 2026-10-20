# ADR-002: Hospital Readiness: Design Validation and Roadmap Beyond Phase 6

**Status:** Proposed
**Date:** 2026-10-19
**Deciders:** Aakash (ftaakash), project owner
**Builds on:** [ADR-001](ADR-001-production-architecture.md), whose Phases 1–6 are implemented. This ADR does not
change ADR-001's architecture. It validates it against use at a real hospital and sets the next phases.

---

## Context

ADR-001 made DischargeFlow production-shaped: a modular monolith on PostgreSQL with row-level security, encrypted PHI, a
hash-chained audit log, governed AI, FHIR R4 and inbound HL7 ADT. It runs on Vercel + Neon with synthetic data. This
ADR asks a different question: what would stop it, or make it unhelpful, in a real hospital?

### Assumptions (a mid-size hospital pilot; estimates for sizing, not measured data)

| | Assumption |
|---|---|
| Size | 400–500 beds, one facility, 20–30 units |
| Volume | ~80–120 discharges a day (average stay about 4–5 days); ~8–12 tasks each, so ~1,000 task updates a day |
| Users | ~1,500–2,500 staff accounts; 150–300 concurrent at peak (shift change, late morning) |
| Systems of record | The EHR (for example Epic or Oracle Health) stays the source of truth for orders, medications and notes. DischargeFlow is the coordination layer. |
| Availability | Discharges happen around the clock, and an outage delays care, so a downtime procedure is required |

### What the review confirmed

- **One transaction per request**, with audit entries and notifications in the same transaction: no partial writes,
  and no audit gaps on rollback.
- **State machine backed by constraints** (one active workflow per patient, one summary per workflow): race conditions
  cannot create duplicate discharges.
- **Tenant isolation in the database** (RLS) rather than in application filters.
- **PHI design:** per-patient keys, crypto-shredding, an HMAC blind index for the MRN, and PHI kept out of logs and
  prompts.
- **AI governance:** de-identification, schema-validated output, grounding warnings, mandatory physician approval,
  provenance and evals.
- **Idempotent HL7 ingestion**, replaying ACKs by control id, as interface engines expect.

### Findings (verified in the code on 2026-10-19)

| # | Severity | Finding | Evidence |
|---|---|---|---|
| 1 | Blocker | Any Google account signs in as `nurse` in the default tenant and can list every patient | `identity/router.py` creates new users with `role="nurse"`, with no invitation or domain check |
| 2 | Blocker | PHI reads are not audited, and neither are patient create/update, workflow initiate/complete or task changes. ADR-001's own target is "every PHI read and write audited". | `write_audit` is called from 19 places, none of them in the patient reads, `discharge` or task routes |
| 3 | Blocker | Any signed-in user can complete or reassign any task, whatever its assigned role | `PUT /api/tasks/{task_id}` requires only a session |
| 4 | High | Insurance claims (create, status change) require only a session | `docs/api.md`, Insurance section |
| 5 | High | Patient and encounter share one row: a readmission overwrites admission date, room and diagnosis, losing the previous stay and making readmission metrics impossible | `Patient` holds `admission_date` and `room_number`; the A01 readmit path reuses the row |
| 6 | High | An A03 that the state machine refuses leaves DischargeFlow showing a patient who has physically left as still admitted, indefinitely | `interop/adt.py` answers AE and changes nothing |
| 7 | High | ADT events needed for correct matching are unsupported: A40 (merge MRNs), A11/A13 (cancel admit/discharge), A02 (transfer, so rooms go stale), A08 (update). Without A40, merged MRNs orphan records. | Only A01 and A03 are accepted |
| 8 | Medium | Point-in-time recovery window on the Neon Free plan is 6 hours | Neon project overview |
| 9 | Medium | The SSE notification stream holds one serverless invocation per open tab (~200 continuously running functions at the assumed peak) | `GET /api/notifications/stream` |
| 10 | Medium | An A03 that arrives before its A01 is answered AE 204, which most interface engines do not retry, so the message is lost | `interop/adt.py` |

Findings 1–4 are addressed by Phase 7, item 1 (PR #48). Findings 6, 7 and 10 are addressed by Phase 8, item 11.
Finding 9 is addressed in code by Phase 9, item 21, for long-running hosts; production on Vercel still polls.

### Workflow gap

DischargeFlow models discharge as a fixed eight-task checklist that a physician starts near the end of the stay:

```
[admitted] ── physician initiates ──▶ 8 fixed tasks ──▶ all done + summary approved ──▶ [discharged]
```

A real discharge is planned from admission and branches on where the patient goes:

```
Admission ─▶ discharge plan (expected discharge date; disposition: home, home health, SNF, rehab, hospice, AMA,
   │         transfer, expired)
   │            ├─ case management: placement, prior authorisation, equipment, transport
   │            └─ daily review of the expected date in rounds, with a delay reason when it slips
   ▼
Medically ready ─▶ orders signed ─▶ medication reconciliation ─▶ teaching in the patient's language ─▶ summary signed
   ▼
Leaves (ADT A03) ─▶ bed to cleaning ─▶ bed available ─▶ summary delivered to the primary care physician or next facility
   ▼
Follow-up: appointment kept? Readmitted within 30 days?
```

Missing today: a disposition (so every patient gets the same tasks), an expected discharge date, task dependencies,
delay reasons, and everything after the patient leaves.

---

## Decision

Keep ADR-001's architecture and add three phases. Each ends deployable, in the same way as ADR-001's phases.

**Phase 7: Pilot readiness.** Nothing here adds features; it makes the system safe to put real patients in.
**Phase 8: Workflow fit.** Model discharge the way hospitals actually run it.
**Phase 9: Integration and value.** Meet clinicians inside the EHR and measure outcomes.

Four design changes carry most of the weight.

### 1. Split Patient and Encounter

`patients` (identity, encrypted identifiers, MRN index) → `encounters` (visit number from PV1-19, admit and discharge
times, unit, bed, disposition, expected discharge date) → `workflows`. The migration copies each patient's current
admission fields into a first encounter and repoints workflows. FHIR `Encounter` then maps to a real encounter, not a
workflow.

### 2. Reconcile A03 instead of refusing it

Replace "AE and change nothing" with "AA, record what happened, flag the gap". A new workflow state
`externally_discharged` records the actual departure time and keeps documentation tasks open with a deadline set by
hospital policy. The patient's status matches the EHR, and the interface engine's queue never stalls on AE responses.

### 3. Role plus relationship authorization, with break-glass

Users see patients on their own units (care team) by default. Reaching any other patient requires a stated reason,
which is audited and reviewed (break-glass). This puts the HIPAA "minimum necessary" rule into practice. It fits the
existing design: `requires(permission)` stays, and RLS enforces unit scope the same way it enforces tenants, through a
per-request setting (`app.unit_ids`).

### 4. Real-time delivery and escalation

On the AWS platform, replace serverless SSE polling with `LISTEN/NOTIFY` fan-out in the long-running API tasks (or a
managed WebSocket service). More important than in-app latency: escalate overdue high-priority tasks to secure
messaging or paging, because clinicians do not watch a web app.

---

## Options Considered

### A03 when the workflow is not ready

| | **Reconcile (chosen)** | Refuse with AE (current) | Force-complete the workflow |
|---|---|---|---|
| Matches the EHR | Yes | No, indefinitely | Yes |
| Documentation integrity | Open tasks stay visible, with a deadline | Unchanged | Lost: tasks closed without being done |
| Interface engine | Never stalls | AE often stalls or drops the queue | Never stalls |
| Complexity | One new state with its own invariants | None | Low, but unsafe |

### Patient/Encounter split

| | **Split now (chosen)** | Keep the combined row |
|---|---|---|
| Readmissions, history, FHIR Encounter | Correct | Overwritten, impossible, approximated |
| Cost | Largest schema change since Phase 1: touches FHIR, analytics, UI | None now |
| Timing | Cost grows with every feature built on the combined model | Debt accumulates |

### Authorization scope

| | **Unit scope + break-glass (chosen)** | Tenant-wide read (current) | Strict per-patient assignment |
|---|---|---|---|
| Minimum necessary | Yes | No | Yes |
| Friction | Float staff use break-glass | None | High: blocks cross-cover |
| Enforcement | RLS, as for tenants | n/a | Hard to keep assignments current |

---

## Consequences

**Easier:** passing a hospital security review (findings 1–4, single sign-on, break-glass, PHI read audit), keeping
DischargeFlow consistent with the EHR, measuring what hospitals care about (length of stay, delay reasons, discharge
before noon, readmissions), and adoption through an in-EHR launch.

**Harder:** the encounter migration touches most modules. The state machine gains a state with different invariants.
Unit scoping adds a dimension to every query and to RLS. Audit volume rises to ~50–100k rows a day with read auditing,
which needs monthly partitioning within a year or two.

**Revisit later:**
- Multi-facility health systems: tenant → facility → unit changes RLS keys and analytics roll-ups.
- Event model: once ADT, escalation and capacity events multiply, use ADR-001's outbox and queue instead of work inside
  request transactions.
- Audit storage: partitioned or append-only storage, exported to the hospital's security system.
- Module boundaries: encounters and capacity may each need their own module; keep the import-linter contracts current.

### Scale check at the assumed load

| Concern | Verdict |
|---|---|
| Writes (~1–2k a day) and reads (low hundreds a minute at peak) | Fine for PostgreSQL. Decrypting PHI per row is the CPU cost, so paginate the patient list. |
| Audit growth with read auditing | Fine for years; partition monthly; plan 6-year retention |
| AI summary latency (synchronous, under Vercel's 60 s limit) | Acceptable for a pilot; needs ADR-001's async worker at scale |
| Live notifications at ~200 concurrent tabs | Weak on serverless (finding 9); on long-running hosts, one LISTEN connection per process (item 21) |
| Availability on Vercel + Neon Free, single region | Not acceptable for production; needs a paid tier or the RDS Multi-AZ plan, plus a downtime procedure |

---

## Action Items

**Phase 7: Pilot readiness**
1. [x] Approval-gated sign-up, task ownership, claim permissions, PHI read audit (findings 1–4; PR #48, migration
   0008, done 2026-10-19).
    a. **Sign-up needs approval.** Before, `POST /api/auth/session` made any Google account a `nurse` in the default tenant, which can read every patient. Now an unknown account gets the role `pending`, which holds no permissions and is refused with 403 by `get_current_user` on every route except `/api/auth/me`. A role with no permissions alone would not have been enough, because many reads (the patient list among them) need no permission. Admins are notified, approve by setting a role (`PUT /api/users/{id}/role`), and can set `pending` again to suspend someone. `ALLOWED_EMAIL_DOMAINS` limits who may request an account, and `SELF_REGISTRATION=false` makes sign-up invite-only. Admins invite with `POST /api/users` (email + role), and the invited person's first sign-in gets that role whatever the domain settings. Existing users, including the production admin, are matched by email (case-insensitive) and keep their roles. The frontend shows a "waiting for approval" screen for `pending`, and the admin screen gains an invite form and a pending notice. **Limits:** the allow-list trusts the email that Emergent's Google login reports. Hospital SSO (item 2: OIDC or SAML against the hospital directory) is the real fix and would also make tenant assignment per hospital.
    b. **Task ownership.** Before, `PUT /api/tasks/{id}` only needed a session, so anyone could complete or reassign any role's task. Now only the assignee (the named user, or anyone in the assigned role) changes the status. Holders of the new `task:override` (physician, admin) may change another role's task with a required reason, stored on the task (`override_reason`, `override_by`). The reason is kept out of the audit entry, because audit rows are immutable, survive erasure, and free text may name the patient. Naming an assignee needs the new `task:assign` (physician, admin), except someone in the task's role claiming it; the assignee must be an approved user. The assistant's `update_task_status` tool goes through the same service function, so the rule holds there too. `/api/tasks/my` now also includes tasks assigned to the user by name.
    c. **Claim permissions.** Creating a claim and changing its status need the new `claim:manage` (nurse, who owns the billing-clearance task, and admin); reads stay open to signed-in users. The assistant's `submit_claim` tool checks the same permission. The insurance screen is read-only for other roles.
    d. **Audit coverage.** Patient create and update (field names only, never values), discharge initiation and completion (including completion by an HL7 A03), task creation, status changes, overrides and reassignments are now audited in the same transaction as the change. Patient-level PHI reads are audited too, with one `PHI_READ` entry per request and no PHI in the details: the patient list (one entry per list, with the record count), patient detail, both workflow-detail routes and the summary read. Reads use resource type `patient` with the patient id, so `GET /api/audit/patients/{id}` answers "who looked at this record". A read that fails is not logged. **Cost:** each audited read appends to the tenant's hash chain, which locks the chain-head row until the request commits, so PHI reads in one tenant now serialize on that lock. The audit call is placed last in each handler to keep the hold short (a few milliseconds plus the commit). At pilot scale this is fine. At hospital scale, read events should move to a separate append-only table, or be batched into the chain by a worker (ADR-001 item 11). SQLite ignores `FOR UPDATE`, so local SQLite request transactions now run one at a time (`core/db.get_db`); without that, concurrent reads appended duplicate sequence numbers, which the Playwright run caught. **Not done:** list endpoints that carry less identifying data (`/workflows`, `/tasks`, `/insurance/claims`) and the assistant's read tools are not read-audited, though every assistant tool call is already audited.
2. [ ] Hospital single sign-on (OIDC or SAML against the hospital directory) with MFA, plus automated provisioning and
   deprovisioning, replacing the Emergent demo login. **Code complete (2026-10-20, migration 0020); awaiting an AWS
   account (Cognito) and a hospital directory to federate with. Not ticked: nothing has signed in through a real
   hospital identity provider yet.**
    - **Per-tenant providers:** `identity_providers` (shared table, like `tenants`; the app role may only read it)
      maps an OIDC issuer and client id to a tenant, so the provider, never the token, decides which hospital someone
      signs in to. Operators add rows with `python -m app.cli sso-provider add` (from the Cognito Terraform outputs);
      the `OIDC_*` variables still configure one provider for the default tenant (id `env`), which is how the ECS task
      is wired. `GET /api/auth/config` lists the active providers.
    - **Sign-in:** the login page runs the authorization-code flow with PKCE (S256 verifier, `state`, `nonce`,
      `prompt=login` so a provider session left on a shared workstation does not let the next person in) and posts
      the code to `POST /api/auth/oidc/callback`; the server redeems it at the token endpoint as a public client and
      verifies the ID token (signature from the provider's JWKS, issuer, audience, expiry, nonce, `token_use`,
      `email_verified`, and `amr` naming a second factor when the provider requires MFA, the default). No provider
      token reaches page scripts. `POST /api/auth/oidc` still takes an ID token directly.
    - **Directory groups → role and units:** `idp_group_mappings` (tenant-scoped, admin API at
      `/api/identity-providers`). Once a provider has a role mapping, every sign-in sets the role from the person's
      groups (most privileged mapped role; none → `pending`), so leaving the directory group removes access at the
      next sign-in; unit mappings replace unit memberships the same way. Unknown people with a mapped role are
      provisioned with it; others become `pending` and admins are notified (403 if `SELF_REGISTRATION=false`). The
      email allow-list does not apply to SSO: the hospital's own directory authenticated them. Changes are audited.
    - **Provisioning and deprovisioning:** a SCIM 2.0 subset at `/api/scim/v2` (Users: create, get, list by
      `userName`/`externalId`, PATCH `active` and profile; ServiceProviderConfig) for an integration client holding the
      new `user:provision`. `active=false` sets `users.deactivated_at`, revokes every session and drops cached
      identities, so the account's requests fail at once in that process and within 30 s elsewhere, and every
      sign-in method refuses it. SCIM is exempt from read-only mode: deprovisioning must not wait for a downtime.
    - **Emergent login:** `EMERGENT_LOGIN=auto` (default) offers the demo Google login only while no hospital provider
      exists; the Terraform stack sets it to `false`. The Vercel deployment keeps it until it moves to AWS.
    - **Cognito (Terraform, `infra/modules/cognito`):** user pool with MFA required for local accounts, self sign-up
      off, SAML and/or OIDC federation from variables (groups mapped into `custom:groups`), hosted-UI domain, public
      app client (code flow, callback `/auth/sso/callback`), outputs for the hospital's identity team (SAML ACS URL
      and audience) and for `sso-provider add`. Passes `terraform fmt`/`validate`; not applied.
    - **Tested** with a fake identity provider (local RSA key as its JWKS, stubbed token endpoint) and SCIM tests:
      tenant from issuer, group sync, PKCE exchange, MFA, deactivation (sessions, refresh, sign-in), tenant
      boundaries, downtime.
    - **Left (needs AWS and a hospital):** apply the Cognito stack, federate it with the hospital directory, point the
      directory's SCIM connector at `/api/scim/v2` with an API key, and verify against the real provider whether
      Cognito passes `amr` through for its federation (`OIDC_REQUIRE_MFA` / `--no-require-mfa`; MFA is enforced at
      the hospital IdP either way). Limits: SCIM PUT, DELETE and Groups are not implemented (connectors deactivate
      with PATCH); a role changed by group mapping takes effect at the next sign-in, not mid-session; identity
      providers are managed by operators from the CLI, not by tenant admins.
3. [x] Unit-scoped access with break-glass (design change 3; migration 0009, done 2026-10-19).
    - **Model:** `units` (code matching HL7 PV1-3.1, so an A01 lands the patient on its unit), `user_units`, `patients.unit_id` (NULL = unassigned) and `access_grants` (break-glass), all tenant-scoped. Admins create units, set memberships and patient units in the admin and patient screens.
    - **Enforcement:** RESTRICTIVE `unit_scope` RLS policies on patients, workflows, tasks, discharge_summaries and insurance_claims, active only when the request sets `app.unit_scope='on'`. The API does that for users whose role lacks the new `patient:read_all` (admin and the ADT `interface` hold it) when `UNIT_SCOPED_ACCESS=true`. Lists, detail routes, FHIR, analytics and the assistant all go through the same policies, so there is one enforcement point, as for tenants.
    - **Break-glass:** a hidden patient answers 403 with `break_glass: true` (a SECURITY DEFINER function tells it from a missing one), and the UI asks for a reason (10–500 characters). The grant lasts 8 hours, the audit entry names the grant (the reason stays on the grant, redacted on erasure), admins are notified and review grants at `GET /api/access-grants` and on the admin screen.
    - **Notifications** about a patient go only to members of the patient's unit, since their text may name the patient.
    - **Rollout decisions:** the flag is off by default, so the deploy changes nobody's access until units are set up. Unassigned patients stay visible to everyone in the tenant, so nothing disappears during setup; new patients default to the creator's first unit.
    - **Limits:** physicians and pharmacists are unit-scoped like nurses; hospitals whose physicians or central pharmacy work across units should give them those units, or grant `patient:read_all` to the role by migration. Break-glass works from a link to the patient (patient id); there is no search across hidden patients yet (the MRN blind index could support one). A hidden workflow link answers 404 rather than offering break-glass. SQLite does not enforce unit scope (tests are `requires_postgres`). Tested on PostgreSQL 16 locally and in CI.
4. [x] Shared-workstation safety: idle lock after a few minutes and fast re-login (tokens already live in memory only).
   After `IDLE_LOCK_MINUTES` (default 5) with no input in any tab, the app unmounts every screen, clears the query
   cache, toasts and in-memory access token, refuses all non-sign-in requests, locks the other tabs, and revokes the
   refresh session. Re-entry is a fresh Google sign-in that returns to the same screen. Resuming the same refresh
   session from the lock screen was rejected: on a shared workstation the next person could press it. The server also
   revokes a session not refreshed for `SESSION_IDLE_MINUTES` (default 20; it can't go below the ~14-minute refresh of
   an open tab), which covers tabs closed or killed before they could lock. Limits: input is detected per browser
   profile, not per person, so someone who keeps using an unlocked screen keeps it open; between closing a tab and
   the server limit, a page reopened in the same profile locks itself using the shared last-activity time, which a
   person at the keyboard could clear from developer tools; re-entry is only as fast as the identity provider, and if
   Google is still signed in on that browser it may re-enter without a password (hospital SSO, item 2, should require
   a fresh login or a badge tap); the URL path of the locked screen (opaque ids, no PHI) stays in the address bar.
5. [ ] Hosting and model provider under a BAA; backups with at least 7 days of point-in-time recovery (finding 8).
   **Code complete (2026-10-20); awaiting an AWS account and a signed BAA. Not ticked: the remaining steps are legal
   and operational (sign the BAA, request Bedrock model access, apply, migrate), and none can be done without the
   account.** The step-by-step go-live checklist is in `infra/README.md` ("Go-live on AWS under a BAA").
    - **Key management:** `core/kms.KmsKeyProvider` implements `core/crypto.KeyProvider` with a KMS symmetric key
      (`PHI_KEY_PROVIDER=kms`, `PHI_KMS_KEY_ID`). Data keys are wrapped by KMS `Encrypt` with a fixed encryption
      context and marked with a prefix; the local KEK becomes a retired key, so existing rows still read and
      `app.cli rewrap-deks` moves them to KMS like any rotation. Unwrapped keys are cached in memory for 5 minutes
      (bounded), so a patient list makes one KMS call per patient, not per field, and repeat reads none. boto3 is
      imported lazily and only by this module. Terraform creates the key (`alias/dischargeflow-<env>-phi`, rotation
      on, 30-day deletion window), grants the ECS task role `kms:Encrypt`/`kms:Decrypt` only with the app's encryption
      context, and sets `PHI_KEY_PROVIDER=kms` in the task. Tested with a fake KMS client.
    - **Backups:** RDS point-in-time recovery is 7 days in staging and 35 in prod (`db_backup_retention_days`); the
      database module now refuses anything under 7. `docs/runbooks/restore-database.md` is the restore procedure.
    - **Model provider:** both environments set `llm_provider = "bedrock"` (Claude on Bedrock, inside the AWS account
      under the BAA); the task role already allows invoking Anthropic models in the region.
    - **What it changes:** with the KEK in KMS, scheduling the key's deletion makes every wrapped data key unreadable,
      including copies in backups, and every unwrap is in CloudTrail. Per-patient erasure is still `Patient.shred()`;
      the blind-index key (MRN lookup) is still derived from `PHI_KEK`, which therefore stays a secret.
    - **Left (needs the account):** sign the AWS BAA in AWS Artifact, request Bedrock model access, apply staging then
      prod, migrate Neon to RDS (copying `PHI_KEK` exactly), run `rewrap-deks` against real KMS, switch DNS, and a
      restore drill. Until then production stays on Vercel + Neon, which is not under a BAA: no real patient data.
6. [x] Downtime procedure: read-only mode, printable discharge census per unit, and a backfill path afterwards
   (migration 0010, done 2026-10-19; procedure in `docs/runbooks/downtime.md`).
    - **Where the flag lives:** a per-tenant `downtime_periods` row that an admin opens and closes
      (`POST /api/downtime/start` / `end`, new permission `downtime:manage`). It is in the database because production
      runs on serverless instances: an environment variable would need a redeploy and could disagree between
      instances, while every instance reads the row on its next write. `READ_ONLY_MODE=true` forces read-only for every
      tenant without touching the database, for restores and failovers.
    - **Enforcement:** one dependency on the whole `/api` router refuses POST, PUT, PATCH and DELETE with 503 and
      `X-Read-Only: true` before the route runs, so a new write route is covered without changes (a test walks every
      route). It reads the tenant from the bearer token or the integration key prefix; only writes pay the one indexed
      query. Exempt: `/api/auth/*` and ending the downtime. Reads keep working and stay audited. HL7 ADT gets the 503,
      so the interface engine queues messages. The frontend polls `/api/downtime/status` (and refetches it on a 503),
      shows a banner on every screen, and disables write controls, the assistant included.
    - **Census:** `GET /api/census` per unit (or unassigned, or all the user can see), JSON or CSV, and a print layout
      at `/census` with a tick box per open task for the paper record. Unit scope (item 3) applies. One `PHI_READ` entry
      per request with the scope and count. CSV cells that start like a formula are neutralised.
    - **Backfill:** `PUT /api/tasks/{id}/backfill` and `PUT /api/workflows/{id}/complete` with `completed_at` record paper
      work at the time it was done. The time must be in the past, after the record was created and inside a recorded
      downtime period; an outage nobody switched read-only for is recorded afterwards (`POST /api/downtime/periods`).
      Ownership rules are those of live updates. `completed_at` keeps the real time (so SLA and duration metrics stay
      right), `backfilled_at` marks the row, and the audit entry names the downtime.
    - **Limits:** the person entering is recorded as `completed_by`, so the paper form stays the source until checked.
      Paper admissions arrive only through the queued ADT feed or manual entry. Summaries can't be generated or approved
      while read-only. If the database itself is down, only a census printed beforehand is available, so units should
      print one at each shift change (a scheduled export is a possible follow-up). Tested on SQLite and PostgreSQL 16.

**Phase 8: Workflow fit**
7. [x] Patient/Encounter split (design change 1).
    - **Built:** `encounters` (migration 0011): one row per stay with the visit number (PV1-19, encrypted with the
      patient's data key), admit and discharge times, unit, room, disposition (PV1-36), status (`active` or
      `discharged`) and a nullable `expected_discharge_date` for item 8. Tenant- and unit-scoped like the other tables
      about a patient (RLS). The database allows one active encounter per patient and one active workflow per
      encounter; `workflows.encounter_id` links a discharge to its stay. The migration gives every patient a first
      encounter from its admission fields and points its workflows to it. A01 opens an encounter (a readmission is a
      new one, and an A01 repeating a visit number already seen is refused), A03 closes the current one through the
      workflow, and refuses one naming another visit. FHIR `Encounter` is the stay. `GET /api/patients/{id}/encounters`
      backs an encounter history on the patient screen; the census reads location and admission from the current
      encounter; `/api/analytics/operations` adds length of stay and 30-day readmissions.
    - **Limits:** stays before the migration were overwritten by readmissions, so each existing patient gets one
      encounter and all its earlier workflows point to it. The patient's `admission_date`, `room_number`, `unit_id` and
      `status` stay as a mirror of the current encounter (the previous release, the patient API and unit-scope RLS read
      them), and `encounter_id` is nullable during the rollout; rows the previous release writes are linked when the
      state machine next touches them. Unit scope still keys on the patient's unit. FHIR Encounter ids changed from
      workflow ids to encounter ids. Tested on SQLite and PostgreSQL 16.
8. [x] Discharge plan from admission: expected discharge date and disposition, reviewed daily (migration 0014).
    - **Built:** the plan is on the encounter: `expected_discharge_date` (from item 7), `planned_disposition` (home,
      home with services, SNF, rehab, hospice, AMA, transfer, expired) and the last review (`plan_reviewed_at`,
      `plan_reviewed_by`). `PUT /api/encounters/{id}/plan` and `POST .../plan/review` need the new `plan:manage`
      (physician, nurse); changing the plan counts as the day's review. `GET /api/discharge-plans/review` lists the
      active stays per unit with "reviewed today" and who reviewed last, not-reviewed first, one `PHI_READ` entry per
      request, under unit scope. "Today" is the date in `HOSPITAL_TIMEZONE` (default UTC). Audit entries carry field
      names, the delay code and ids, never the dates. The patient screen has a plan card (edit, "reviewed, no
      change", delays), and `/plans` is the rounds list.
    - **Limits:** the plan is set by hand; nothing reads an expected date from the EHR or the ADT feed yet (PV2-9
      could). Disposition codes are DischargeFlow's own list, not mapped to HL7 PV1-36 codes (the actual disposition
      from A03 stays in `disposition`). The review list is not paginated (fine for a few hundred beds).
9. [x] Workflow templates per disposition, with task dependencies and due times relative to the expected date.
    - **Built:** `workflow_templates` (tenant-scoped): one per disposition, tasks with a key, role, priority, due time
      in hours from 00:00 on the expected date (hospital time; negative is the days before) and prerequisites by key.
      Migration 0014 seeds home, home with services, SNF, rehab, hospice and AMA into every tenant.
      `initiate_discharge` uses the active template for the planned disposition and records its id and version on the
      workflow; tasks get `due_at`, `due_offset_hours` and `depends_on` (task ids). `update_task` refuses to start or
      complete a task while a prerequisite is open (409, overrides included), and moving the expected date moves the
      open tasks' due times. Admins manage templates at `/api/workflow-templates` (`template:manage`) and on the admin
      screen; saving bumps the version and is audited. The workflow screen shows due times, overdue tasks and what a
      task is waiting on. Escalation (item 12) and the SLA metrics use the same due time: the template's when a
      task has one, else the SLA target for its priority after creation.
    - **Limits:** without a plan, or without an active template for the disposition, the eight standard tasks (no
      prerequisites, SLA by priority) are used, as before; the E2E path still runs that way. A disposition changed
      after the discharge started does not re-template it. Approving the AI summary completes the summary task
      directly, so it should have no prerequisites (none of the defaults do). A template task created after its due
      time gets its priority's SLA from creation instead, so a late start isn't counted as a breach by staff.
10. [x] Delay reason codes whenever the expected date slips or a task breaches its SLA.
    - **Built:** `delay_reasons` (tenant- and unit-scoped): kind (`expected_date_slip`, `task_sla_breach`), code
      (medical, social, placement, insurance, transport, pharmacy, documentation, other), optional note (required for
      other), `slip_days`, who and when. Required when the date moves later, on the first review of a day after the date
      has passed, and when a task is completed after its due time (also on backfill and through the assistant's tool),
      unless one is on record (`POST /api/tasks/{id}/delay-reason` records it while the task is open). Audited with the
      code and ids; the note stays on the row and erasure redacts it. `/api/analytics/operations` adds `delays`: counts
      by kind and code, with average slip days only for groups of three or more. The task SLA breach count now uses a
      task's template due time when it has one.
    - **Limits:** one code per event, no hierarchy (no "placement: SNF bed vs. insurance denial" subcodes) and no
      attribution to a service or unit in the metrics yet (Phase 9 item 19). A late summary task completed by approval
      asks for no reason.
11. [x] A03 reconciliation (design change 2), the remaining ADT events (A02, A08, A11, A13, A40 with MRN merge), and
    parking of out-of-order messages (findings 6, 7, 10).
    - **Built:** an A03 the state machine refuses is now AA: the encounter closes at the departure time (PV1-45, else
      EVN-6, EVN-2, MSH-7), the patient is discharged, and the workflow becomes `externally_discharged` with
      `departed_at` and `documentation_due_at` (departure plus `ADT_DOCUMENTATION_HOURS`, default 24). Its open tasks
      stay open; physicians and the unit's nurses are notified; the audit entry carries ids, times and the open-task
      count. It is not an active status, so a readmission can start a new stay meanwhile, and `complete_discharge`
      closes it once the tasks are done and the summary approved, at the departure time, without touching the new
      stay. An A03 with no workflow closes the stay and tells physicians. A02 moves the patient and the current
      encounter to the unit and room in PV1-3. A08 updates a whitelist (name, date of birth, attending, diagnosis).
      A11 marks the encounter `cancelled` (a cancelled visit number can be admitted again). A13 reopens the last
      stay and sends an `externally_discharged` workflow back to in progress. A40 merges the MRG-1 record into the
      PID-3 one (`patients/merge.py`): encounters, workflows, tasks, summaries, claims, grants and HL7 records move,
      visit numbers and summary texts are re-encrypted with the survivor's data key, the old record keeps
      `merged_into` and loses its MRN blind index, and both records get a `MERGE_PATIENT` audit entry; when only the
      MRG-1 record is known it simply takes the surviving MRN. Events for an MRN not seen yet are parked
      (`hl7_parked_messages`, migration 0015): AA with a note, the message stored only as AES-GCM ciphertext under its
      own wrapped key, replayed in arrival order when the A01 arrives, and expired after `HL7_PARK_HOURS` (default 72)
      with admins notified. Admins list and discard them at `/api/hl7/parked`; the ciphertext is dropped once a
      message is replayed, expired or discarded. FHIR `Patient.link` points a merged record to its survivor.
      Tested on SQLite and PostgreSQL 16 (tenant isolation of parked messages).
    - **Limits:** a task that no longer applies once the patient has left (for example patient education) can only be
      closed through the existing override with a reason; there is no "not done" task status yet. Overdue
      documentation is visible on the workflow (`documentation_due_at`) but not escalated; item 12 covers escalation.
      A11 is refused (AE, physicians and admins notified) when a discharge workflow exists for the admission, since
      workflows can't be cancelled. A40 is refused when both records have a stay or a discharge in progress, or one
      was erased; admins merge those by hand. A08 ignores location (A02's job) and fields outside the whitelist, and
      an A08 for an outpatient MRN DischargeFlow never admits is parked until it expires. Times without a UTC offset
      are taken as UTC. Expiry runs on the next ADT request or admin listing, not on a timer. Parking only covers
      an unknown MRN; other out-of-order cases (an A03 for a stay whose A01 readmission has not arrived) are still
      decided against the current state. Tables added by unmerged items (summary versions and addenda, item 13)
      are not moved by a merge yet.
12. [x] Escalation of overdue tasks to secure messaging or paging (design change 4; migration 0012, done 2026-10-19).
    - **Policy:** a task breaches at its SLA target (`TASK_SLA_HOURS`, the operational metrics' targets: high 4 h,
      medium 12 h, low 24 h). Level 1, at the breach, notifies the assignee (the named user, or the assigned role on
      the patient's unit). Level 2, a further 1 h (high) or 4 h (medium) later, and never sooner than that after level
      1, notifies the supervising role. Low priority stops at level 1. High priority is paged at both levels, medium
      at level 2 only, low never: paging for routine work trains people to ignore pages.
    - **State:** one `escalations` row per step (tenant-scoped, usual policy), unique per task and level, with ids,
      category, priority, unit, who was told and how many, and page status. No PHI. Completed tasks resolve their
      escalations on the next run.
    - **Running it:** there is no scheduler on Vercel, so `/api/escalations/run` is idempotent and something calls
      it: Vercel Cron (GET with `CRON_SECRET`; off until that is set), a hospital scheduler as an integration
      client (`escalation:run`, the intended cadence: every few minutes), an admin's "Run now", or `python -m app.cli escalate`. Each run takes
      only the steps that are due and missing, at most one new step per task (so level 2 never fires in the same run
      as level 1), and is bounded per run (100 escalations, 20 pages) to stay inside the function time limit. On
      Postgres a per-tenant advisory lock makes an overlapping run skip; the unique constraint backs it up. A tenant
      in read-only mode is skipped, since nobody can complete tasks during a downtime.
    - **Delivery:** in-app notifications always, through the same unit-filtered fan-out as other notifications. A
      `Pager` interface adds the external channel: `none` (default) or a generic webhook to the hospital's
      secure-messaging or paging gateway, HMAC-SHA256 signed over timestamp and body, with an idempotency key stable
      per task and level. Failed pages are retried on the next runs, up to 3 attempts. Pages and notifications carry
      task id, category, priority, unit code, minutes overdue and a link; the recipient signs in to see the patient,
      so unit scope and PHI-read audit still apply.
    - **Audit and view:** every step is audited (`ESCALATE_TASK`) and every page attempt (`PAGE_ESCALATION`). The
      admin screen lists active escalations with who was told (a count of 0 flags a unit nobody in that role is on),
      page status, the policy, and whether a schedule is configured.
    - **Limits:** there is no charge-nurse role, so nursing and pharmacy tasks escalate to physicians and physician
      tasks to admins; a real charge-nurse or house-supervisor role (and an on-call roster) is the hospital-specific
      follow-up. The policy is code, not per-hospital configuration. Delivery is at least once: a run whose commit
      fails pages again, which the idempotency key lets the gateway drop. The Vercel project is on the Hobby plan,
      which refuses to deploy a cron job that runs more than once a day, so `vercel.json` schedules a daily run
      (07:00 UTC, within the hour) as a backstop only. That is far too slow for a 4-hour target: until the project
      moves to Pro (then `*/5 * * * *`), something else must call the run every few minutes, such as the hospital's
      interface engine or scheduler with an integration key. Vercel Cron runs only on production deployments; on AWS a scheduled task (EventBridge) replaces it, and the outbox (ADR-001 item 11) would make page
      delivery asynchronous. Paging is not acknowledged back: a page is "sent" once the gateway accepts it, and only
      completing the task stops further levels. No gateway has been tested yet; the webhook is tested against a
      local fake. Tested on SQLite and PostgreSQL 16.
13. [x] Discharge summary lifecycle: signed versions, addenda instead of edits after signature, and a sign-by deadline
    tracked against hospital policy.
    - **Signed versions:** the physician's approval is the signature. It freezes the text as
      `discharge_summary_versions` version 1 (migration 0013) with `signed_by`, `signed_at` and a SHA-256 of the signed
      text; reads report `hash_verified`. From then on the summary's text can't change: the API refuses a second
      approval, and the model raises `SignedSummaryError` if code tries to set different text. Summaries signed before
      0013 get their version 1 from `app.cli migrate`.
    - **Addenda:** `POST /api/ai/summaries/{id}/addenda` (`summary:manage`) appends text to the latest signed version,
      signed by its author as it is added (`signed_by`, `signed_at`, SHA-256), including after discharge. Never edited
      or removed. The summary read and the workflow view show the signed version, the version history and the
      addenda; FHIR `DocumentReference` carries the signed version and then one attachment per addendum, with
      `docStatus` `amended`.
    - **Protection:** versions and addenda are encrypted with the patient's data key (AAD: their own id and field),
      tenant-scoped, and unit-scoped like `discharge_summaries`. Erasure overwrites their text and clears their hashes.
      Audit entries carry version and addendum ids only.
    - **Sign-by deadline:** `SUMMARY_SIGN_HOURS` (default 24, 1 to 720) after the patient left. While the patient is
      still here the expected discharge time stands in, so a stalled unsigned discharge still becomes overdue.
      Operational metrics report overdue unsigned summaries, time to sign and signatures after the deadline;
      `GET /api/ai/summaries/overdue` lists them (ids and times only) and the workflow view flags them.
      `python -m app.cli summary-deadlines` (or `POST /api/ai/summaries/overdue/notify`) notifies physicians on the
      patient's unit once per discharge.
    - **Limits:** nothing runs the notification sweep on its own yet: on ECS it needs a scheduled task (hourly is
      enough; the sweep is idempotent), and on Vercel a Vercel Cron calling the endpoint with a service identity.
      Until then overdue summaries show in metrics and in the workflow view only. Discharge still requires a signed
      summary, so "after discharge" deadlines matter only once A03 reconciliation (item 11) lets a patient leave
      first. There is one signer per version (no co-signature by an attending for a resident's summary) and no
      correction workflow that issues version 2; the version number leaves room for both. The signature is the
      authenticated approval plus a hash, not a cryptographic signature with the physician's own key. Tested on
      SQLite and PostgreSQL 16.

**Phase 9: Integration and value**
14. [x] SMART on FHIR launch from the EHR, with patient context and no second sign-in.
    - **Flow:** SMART App Launch EHR launch as a public client with PKCE (S256). The EHR opens `/launch?iss&launch`;
      the server checks `iss` against a per-tenant allow-list (`smart_issuers`, migration 0018, admin screen) before
      contacting it, discovers the endpoints from `.well-known/smart-configuration`, and returns the authorization
      URL (scopes `launch openid fhirUser patient/*.read`, a fresh `state` stored as a hash, a nonce). On the way back
      the browser checks `state` against the one it kept and `POST /api/smart/token` exchanges the code (single use,
      verifier checked against the stored challenge) and verifies the id_token against the server's JWKS (issuer,
      audience, expiry, nonce, `fhirUser`).
    - **Who:** an admin link from the EHR user (`fhirUser`) to a DischargeFlow user, else a match by email (from the
      id_token or the Practitioner's telecom) with the same rules as the Google login: unknown accounts become
      `pending`, registration limits apply. The result is our normal session (15-minute token, `df_refresh` cookie),
      so the idle lock, unit scope and audit all apply unchanged.
    - **Which patient:** the EHR Patient's MRN identifier (configured system, else the one typed `MR`) is looked up
      through the MRN blind index with the configured assigning authority, and the encounter by its visit number,
      else the active stay. The browser lands on the active workflow, else the patient. No match still signs in, with
      the reason shown.
    - **The EHR token** never reaches the browser: it is stored encrypted under the launch's own data key, capped at an
      hour, without a refresh token, and usable only by the session that made the launch. Expiry, a 401 from the EHR
      or disabling the issuer drops it. Launches are audited (`SMART_LAUNCH`, `SMART_LAUNCH_FAILED`, ids and reasons
      only).
    - **Limits:** tested only against a fake EHR (unit tests with a fake authorization and FHIR server, and a
      Playwright spec with a local fake EHR), not a real Epic or Oracle Health sandbox, so vendor quirks (Epic's
      identifier systems, `fhirUser` formats, required `aud`) are untested. Public client only: no confidential
      client secret or private-key JWT client authentication, and no standalone launch. Launches go to the default
      tenant, as the Google login does. The link from EHR user to DischargeFlow user is by email or an admin link
      created through the API; the admin screen registers EHRs but does not manage links yet. Signing out of
      DischargeFlow does not revoke the EHR token at the EHR; it is simply no longer usable by any session.
15. [x] Read-only clinical context from the EHR over FHIR (medications, problems, allergies), so medication
    reconciliation compares real lists and summaries are grounded in real data.
    - **Read:** `GET /api/smart/clinical-context/{patient_id}` reads active `MedicationRequest`, `Condition` and
      `AllergyIntolerance` live from the EHR with the session's launch token (httpx, 8-second timeout, only the
      launch's `iss`, at most five pages). Parsed lists are cached 60 seconds per process and never persisted.
      Entered-in-error, refuted, inactive and resolved entries are left out. Each response with data is a `PHI_READ`.
    - **Shown:** on the workflow screen, and on medication tasks in the task list (medications and allergies), where
      the pharmacist reconciles against the real lists.
    - **Summaries:** the same lists, de-identified, become `ehr_active_medications`, `ehr_problem_list` and
      `ehr_allergies` in the facts (prompt `discharge_summary@v2` tells the model to reconcile against them).
      Grounding checks against what the model was given, and a discharge medication named like a recorded allergy
      adds an `allergy` warning. Two golden-set cases (reconciliation, de-identification of EHR text) keep the evals
      green on the mock adapter.
    - **Limits:** the context exists only while the physician's session holds a live EHR launch for that patient;
      there is no backend service access (SMART Backend Services) for reading without a launch. Medications are
      `MedicationRequest`s only (no `MedicationStatement` or dispense history), names are matched lexically, and the
      allergy check would not know that amoxicillin is a penicillin. Not tested against a real EHR sandbox.
16. [ ] Outbound summary delivery (C-CDA or a FHIR document) to the primary care physician or receiving facility, with
    delivery tracked.
17. [x] Capacity board: expected discharges by unit and hour, discharge before noon, and the bed-to-cleaning handoff
    (migration 0019, done 2026-10-19).
    - **Built:** `GET /api/capacity/board` (`capacity:read`: physician, nurse, admin) and the `/capacity` screen. Per
      unit and for the hospital: patients in house, discharges expected that day from the plan's expected date (item
      8), by hour, plus tomorrow's, stays past their expected date and stays with none; discharges done that day by
      hour, and the before-noon rate over 7 days (hospital time). Beds: `bed_turnovers` opens a `dirty` turnover when a
      stay ends in a bed, whichever way it ends (UI completion, manual status change, ADT A03 with or without a
      workflow, reconciliation to `externally_discharged`), and when an A02 moves a patient out; an A13 cancels a
      turnover nobody has started. Charge nurses, admins and the EVS system (an integration client with `bed:manage`)
      move it to `cleaning` and `available`; each step records the time and who, and is audited. Unit scope applies:
      RLS on `bed_turnovers` by its own unit, and the board lists only the user's units. The board shows counts and
      bed locations, never patients.
    - **Left:** the plan has a date and no time, so the hour comes from the discharge workflow's estimated time, which
      is a placeholder (start plus 2 hours) until someone sets it; plans without one are counted as "no time". A bed
      is a unit and room string, as the ADT feed sends it: there is no bed master, so there is no count of empty beds
      or occupancy percentage, and a room with two beds is one location unless the feed names the bed. There is no EVS
      role for people: EVS staff work through their own system's API key, or a charge nurse advances beds. No
      notification goes to EVS when a bed becomes dirty. Tested on SQLite; the unit-scope test runs on PostgreSQL 16
      in CI.
18. [x] Patient-facing discharge instructions in plain language and the patient's language, approved by a clinician.
    - **Source:** drafted only from the signed summary (item 13): its latest signed version and that version's
      addenda, never the unsigned draft. `source_sha256` records exactly what was used, so an addendum signed later
      marks the instructions `stale` until someone redrafts or rechecks them. Discharge summaries must be signed
      first; the API refuses otherwise (409).
    - **Governed generation:** the same path as summaries. The signed text is de-identified, sent with the target
      language and reading grade under a new versioned prompt (`patient_instructions@v1`, hash pinned in the tests),
      and the model must return JSON matching a schema (what happened, conditions, medicines, warning signs,
      follow-up, activity and diet, with section headings in the patient's language). Invalid output gets one retry,
      then a visible 502 with the failed attempt's provenance kept. Every call adds an `ai_generations` row.
    - **Grounding:** every medicine name and condition (which the model copies untranslated next to its plain
      explanation) must appear in the signed text, as must any known drug name anywhere in the text; a discharge
      medication in the summary that the instructions leave out is flagged as an omission. Warnings are shown to the
      reviewer.
    - **Readability:** a deterministic Flesch-Kincaid grade is stored with the draft and with the approved text.
      `PATIENT_INSTRUCTIONS_TARGET_GRADE` (default 6, a common health-literacy target) sets the goal: an English
      draft above it is retried once with a request to simplify, and flagged if it is still above. The formula is
      English only, so other languages get no grade rather than a misleading one.
    - **Language:** `patients.preferred_language` (migration 0017) holds a BCP 47 code, editable on the patient page;
      instructions default to it and can be drafted in any other language too, one set per language. It is stored in
      clear: a language is not an identifier on its own.
    - **Review and print:** a nurse or physician (`instructions:manage`) edits and approves the draft; the edit diff
      and similarity are stored as for summaries, and approved text can't change. Only approved instructions print,
      from a printable page (`/instructions/:id/print`, black on white, the patient's name, `lang` and right-to-left
      layout set) whose load is an audited PHI read.
    - **Protection:** the text is encrypted with the patient's data key like summaries, tenant- and unit-scoped,
      overwritten on erasure, and re-keyed when an A40 merge moves it. Audit entries carry ids, language and counts,
      never text.
    - **Evals:** the golden set has four instruction cases (pneumonia, heart failure with an addendum, a prompt
      injection inside the signed hospital course, a Spanish request) checking for invented or omitted medicines, forbidden text,
      identifiers sent to the model, coverage, and English drafts above the target grade.
    - **Limits:** translation quality depends entirely on the model, and nothing here measures it. CI runs the
      offline mock, which writes English whatever language is asked, so the non-English eval case checks grounding
      and de-identification only. Before patients receive translated instructions, a hospital needs a qualified
      reviewer for each language it offers (or certified translation) and a live eval with the chosen model; the
      reviewing nurse or physician may not read the language. Flesch-Kincaid is a rough proxy for plain language and
      says nothing about layout, numeracy or cultural fit. There is no revision workflow for approved instructions
      (a stale approved set can't be redrafted in the same language), no patient portal or electronic delivery
      (printed handout only; item 16 covers clinician delivery), the language is not read from HL7 PID-15 yet, and
      no real model has drafted instructions here (no BAA-covered provider yet). Tested on SQLite and in CI on
      PostgreSQL 16.
19. [x] Outcome analytics: length of stay against expected, accuracy of the expected discharge date, delay reasons,
    discharge before noon, 30-day readmissions (needs item 7). Done 2026-10-19.
    - **Built:** `GET /api/analytics/outcomes?days=` (`insights:read`) and an Outcomes panel on `/insights`. For stays
      discharged in the window, overall and by unit: length of stay, discharge day against the final expected date,
      accuracy of the final and of the first expected date (`encounters.first_expected_discharge_date`, kept when
      the date moves), discharge before noon, and 30-day readmission (another admission of the same patient within 30
      days, counted only once the 30 days have passed). Delay reasons from item 10 as a Pareto (count, share,
      cumulative share), by kind and by unit. The same rules as the other aggregates: counts and durations only, and
      groups under 3 show counts without averages or rates.
    - **Left:** attribution is by unit only. There is no service line (medicine, surgery) or attending in a usable
      form in the data model, so "by service" waits for that field. The first expected date is recorded from
      migration 0019 on; older plans only count toward the final date, because a date moved earlier records no delay
      reason and the first value can't be reconstructed. A readmission is any later admission in this system, planned
      or not, at any unit; admissions to other hospitals are not seen. Not yet fed to the AI insights model.
20. [ ] Readmission risk flag at planning time from a published, validated score over structured data, not an LLM,
    so it stays explainable and auditable.
21. [x] Real-time delivery without serverless polling once on AWS (finding 9, design change 4; migration 0016, done
    2026-10-19 in code; production on Vercel still polls until the AWS move).
    - **Built:** an AFTER INSERT trigger on `notifications` calls `pg_notify('notifications',
      '<tenant>:<user>:<notification id>')`; Postgres delivers it only when the inserting transaction commits, so a
      rolled-back notification is never announced. With `NOTIFICATION_TRANSPORT=listen`, each API process holds one
      dedicated asyncpg connection with `LISTEN notifications` (`notifications/hub.py`), on `DATABASE_DIRECT_URL` or
      a direct URL derived from `DATABASE_URL` (a Neon `-pooler` host loses the suffix; another pooled URL without
      `DATABASE_DIRECT_URL` falls back to polling), and hands each id to the open streams of exactly that tenant and
      user. A stream then makes no query per event; it re-checks the auth session every 15 s, so logout still ends it.
      Ids only, as before: no notification text goes over the channel or the stream.
    - **Failure handling:** the hub reconnects with backoff (1 s doubling to 30 s) and pings every 30 s to catch a
      silently dropped connection. After a reconnect, every open stream sends another `ready`, which the client
      already treats as "refetch the list", so notifications committed during the gap are not lost. A stream that
      can't get a connected hub within 3 s polls instead, as does every stream on SQLite.
    - **Where it runs:** `listen` is set (with 5-minute streams, kept open through the ALB's 60 s idle timeout by 15 s
      heartbeats) in the ECS task definition, docker-compose and the k8s Deployment. `poll` stays the default and is
      what Vercel uses: a serverless function has no long-lived process to hold a LISTEN, and the per-tab function
      cost of finding 9 remains there until the AWS move.
    - **Tests:** on Postgres, a committed insert reaches the subscriber and a rolled-back one does not (with polling
      disabled, so the event can only come over LISTEN), tenant isolation of the fan-out, and revocation ending a
      listen stream. Everywhere: fan-out to only that tenant and user, queue overflow collapsing to one resync,
      reconnect with resync (fake connection), fallback to polling on SQLite and when the hub can't connect.
    - **Limits:** not run against RDS or under load (no AWS account). Each process costs one extra database
      connection. Fan-out is per process, which is enough because every process listens; a fleet large enough to
      strain one LISTEN per task would move to a broker.
