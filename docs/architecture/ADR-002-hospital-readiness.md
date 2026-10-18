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

Findings 1–4 are addressed by PR #48 (Phase 7 in ADR-001), open at the time of writing.

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
1. [ ] Approval-gated sign-up, task ownership, claim permissions, PHI read audit (findings 1–4). In PR #48.
2. [ ] Hospital single sign-on (OIDC or SAML against the hospital directory) with MFA, plus automated provisioning and
   deprovisioning, replacing the Emergent demo login.
3. [ ] Unit-scoped access with break-glass (design change 3).
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
