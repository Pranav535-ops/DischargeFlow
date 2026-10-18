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

Findings 1–4 are addressed by Phase 7, item 1 (PR #48).

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
| Live notifications at ~200 concurrent tabs | Weak on serverless (finding 9) |
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
   deprovisioning, replacing the Emergent demo login.
3. [x] Unit-scoped access with break-glass (design change 3; migration 0009, done 2026-10-19).
    - **Model:** `units` (code matching HL7 PV1-3.1, so an A01 lands the patient on its unit), `user_units`, `patients.unit_id` (NULL = unassigned) and `access_grants` (break-glass), all tenant-scoped. Admins create units, set memberships and patient units in the admin and patient screens.
    - **Enforcement:** RESTRICTIVE `unit_scope` RLS policies on patients, workflows, tasks, discharge_summaries and insurance_claims, active only when the request sets `app.unit_scope='on'`. The API does that for users whose role lacks the new `patient:read_all` (admin and the ADT `interface` hold it) when `UNIT_SCOPED_ACCESS=true`. Lists, detail routes, FHIR, analytics and the assistant all go through the same policies, so there is one enforcement point, as for tenants.
    - **Break-glass:** a hidden patient answers 403 with `break_glass: true` (a SECURITY DEFINER function tells it from a missing one), and the UI asks for a reason (10–500 characters). The grant lasts 8 hours, the audit entry names the grant (the reason stays on the grant, redacted on erasure), admins are notified and review grants at `GET /api/access-grants` and on the admin screen.
    - **Notifications** about a patient go only to members of the patient's unit, since their text may name the patient.
    - **Rollout decisions:** the flag is off by default, so the deploy changes nobody's access until units are set up. Unassigned patients stay visible to everyone in the tenant, so nothing disappears during setup; new patients default to the creator's first unit.
    - **Limits:** physicians and pharmacists are unit-scoped like nurses; hospitals whose physicians or central pharmacy work across units should give them those units, or grant `patient:read_all` to the role by migration. Break-glass works from a link to the patient (patient id); there is no search across hidden patients yet (the MRN blind index could support one). A hidden workflow link answers 404 rather than offering break-glass. SQLite does not enforce unit scope (tests are `requires_postgres`). Tested on PostgreSQL 16 locally and in CI.
4. [ ] Shared-workstation safety: idle lock after a few minutes and fast re-login (tokens already live in memory only).
5. [ ] Hosting and model provider under a BAA; backups with at least 7 days of point-in-time recovery (finding 8).
6. [ ] Downtime procedure: read-only mode, printable discharge census per unit, and a backfill path afterwards.

**Phase 8: Workflow fit**
7. [ ] Patient/Encounter split (design change 1).
8. [ ] Discharge plan from admission: expected discharge date and disposition, reviewed daily.
9. [ ] Workflow templates per disposition, with task dependencies and due times relative to the expected date.
10. [ ] Delay reason codes whenever the expected date slips or a task breaches its SLA.
11. [ ] A03 reconciliation (design change 2), the remaining ADT events (A02, A08, A11, A13, A40 with MRN merge), and
    parking of out-of-order messages (findings 6, 7, 10).
12. [ ] Escalation of overdue tasks to secure messaging or paging (design change 4).
13. [ ] Discharge summary lifecycle: signed versions, addenda instead of edits after signature, and a sign-by deadline
    tracked against hospital policy.

**Phase 9: Integration and value**
14. [ ] SMART on FHIR launch from the EHR, with patient context and no second sign-in.
15. [ ] Read-only clinical context from the EHR over FHIR (medications, problems, allergies), so medication
    reconciliation compares real lists and summaries are grounded in real data.
16. [ ] Outbound summary delivery (C-CDA or a FHIR document) to the primary care physician or receiving facility, with
    delivery tracked.
17. [ ] Capacity board: expected discharges by unit and hour, discharge before noon, and the bed-to-cleaning handoff.
18. [ ] Patient-facing discharge instructions in plain language and the patient's language, approved by a clinician.
19. [ ] Outcome analytics: length of stay against expected, accuracy of the expected discharge date, delay reasons,
    discharge before noon, 30-day readmissions (needs item 7).
20. [ ] Readmission risk flag at planning time from a published, validated score over structured data, not an LLM,
    so it stays explainable and auditable.
21. [ ] Real-time delivery without serverless polling once on AWS (finding 9, design change 4).
