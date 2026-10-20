# End-to-end test plan: DischargeFlow in a simulated hospital (Vercel + Neon)

**Goal:** show, with evidence, whether `main` as deployed on Vercel + Neon works and performs under the load and
behaviour of a real hospital, using synthetic data only.
**Scope:** the production stack as it is: Vercel Hobby (Python function + static SPA) and Neon Free (`dischargeflow-db`).
**Load model:** the ADR-002 pilot assumptions, which are sizing estimates, not measurements:
- 400–500 beds, 20–30 units
- 80–120 discharges a day, ~1,000 task updates a day
- 150–300 concurrent users at peak

## 1. Constraints that shape the plan

| Constraint | Consequence for testing |
|---|---|
| Production forces `ENABLE_DEV_LOGIN` off and signs in through Emergent Google OAuth. | Automated multi-role journeys and load can't sign in to production. They run on a production-like **simulation environment** (§2); production gets unauthenticated smoke tests, synthetic monitoring and a small manual pilot. |
| No real PHI is allowed (no BAA, and Neon Free keeps 6 h of point-in-time recovery). | Synthetic patients only (Synthea-style or the project's generator). Tests assert that no PHI patterns appear in logs. |
| Vercel Hobby allows a cron job **once a day**: `vercel.json` runs escalations at 07:00 and deliveries at 07:30. | In a real hospital, overdue tasks would escalate up to 24 h late. Test the trigger cadence explicitly (§4.F); a GitHub Actions schedule calling `POST /api/escalations/run` every 5–10 min is the free workaround to evaluate. |
| Neon Free: small compute that auto-suspends when idle, a monthly compute-hours quota, 10 branches. | Measure cold-start latency. Budget the CU-hours each simulated day uses. The simulation branch counts toward the 10. |
| Serverless: 60 s function limit, no long-lived connections, SSE streams poll every 5 s. | AI summaries must finish in under 60 s. Measure notification latency (expected up to 5 s) and the function invocations SSE generates. |
| Hobby is licensed for non-commercial use. | Fine for evaluation. A paying hospital pilot needs Vercel Pro (and a BAA-capable host). Record this as an exit-criteria finding, not a test. |

## 2. Environments

| Environment | What it is | Used for |
|---|---|---|
| **Production** (`dischargeflow.vercel.app`, Neon `main`) | Real deployment | Smoke, synthetic monitoring, security headers, one manual clinician walkthrough on a synthetic tenant |
| **Hospital simulation** (`sim`) | A long-lived git branch `sim`. Vercel preview env vars scoped to it: `APP_ENV=test`, `ENABLE_DEV_LOGIN=true`, `LLM_PROVIDER=mock` (or `anthropic` + Haiku for AI runs), `UNIT_SCOPED_ACCESS=true`, `AUTO_MIGRATE=true`, its own `JWT_SECRET`/`PHI_KEK`. Neon branch `preview/sim`. | Every automated suite below: journeys, HL7 replay, load, concurrency, resilience |
| **CI** (GitHub Actions, Postgres 16 service) | Existing | Unit/API/RLS tests, migrations, evals, Playwright on a local build |

The `sim` branch is code-identical to `main`: rebase it on `main` before each run, and add no code changes. Only
environment variables differ, so the results transfer to production.

## 3. The simulated hospital

**Seed (one-off script, idempotent):**
- one tenant, 24 units of 18–20 beds
- ~430 occupied beds with synthetic patients and encounters
- expected discharge dates spread over the next 1–6 days
- dispositions distributed as: home 60%, home health 15%, SNF 12%, rehab 5%, hospice 3%, AMA 2%, transfer 3%
- users: 120 nurses, 40 physicians, 12 pharmacists, 3 admins, each assigned to units (the app has no case-manager role; nurses plan and review discharges)
- one integration client (ADT feed)

**A simulated day (driver script, compressible 24 h → 2 h):**

| Time | Event stream |
|---|---|
| 00:00–06:00 | Low ADT trickle (A01 admissions from ED, A02 transfers); night nurses complete tasks |
| 07:00 | Shift change: login burst, census and plan views by every unit |
| 08:00–11:00 | Rounds: plan review on every active encounter, EDD changes with delay codes, physicians initiate 80–120 discharges, AI summaries generated |
| 10:00–14:00 | **Discharge peak:** task completions (~70% of the day's ~1,000), summary approvals, discharge completions, A03s from ADT (some before the paperwork is done), capacity board refreshes |
| 14:00–19:00 | Late discharges, escalations, deliveries, readmissions (~10% of discharges come back as A01 within the window) |
| 19:00 | Second shift change |

**Chaos sprinkled in:**
- 3% duplicate HL7 messages
- 2% A03 arriving before its A01
- 1% A40 merges
- 1% A11/A13 cancellations
- concurrent edits on the same workflow
- one 20-minute read-only downtime

## 4. Test suites

Status: ✅ exists today · ◑ partly exists · ❌ gap to build.

### A. Functional end-to-end journeys (Playwright, against `sim`)

The existing specs run in CI only. Point them at `sim` with `baseURL` and run them there too.

| # | Journey | Status |
|---|---|---|
| A1 | Admit → initiate → AI draft → edit/sign → nurse and pharmacist tasks → complete → FHIR read-back | ✅ `discharge.spec.js` |
| A2 | Assistant proposes, then the user confirms or cancels a write | ✅ `assistant.spec.js` |
| A3 | Read-only downtime: writes blocked, census prints, backfill afterwards | ◑ `downtime.spec.js` (backfill not covered) |
| A4 | Idle lock on a shared workstation | ✅ `idle-lock.spec.js` |
| A5 | SMART launch with EHR lists | ◑ fake EHR only; add the public SMART Health IT sandbox (synthetic data) |
| A6 | Disposition template (SNF): dependencies block out-of-order completion; due times follow EDD changes | ❌ |
| A7 | EDD slips, a delay code is required, and it shows in outcomes | ❌ |
| A8 | Patient leaves first (A03): `externally_discharged`, documentation deadline, completion later | ❌ UI-level |
| A9 | Unit scope: a nurse can't see another unit; break-glass with a reason works; the privacy officer sees the grant | ❌ UI-level |
| A10 | Overdue task escalates (level 1, then 2) and the notification arrives live over SSE | ❌ |
| A11 | Signed summary: an addendum is added, delivery is queued → sent, and a failure appears on the worklist | ❌ |
| A12 | Patient instructions: draft, readability shown, clinician approves, print | ❌ |
| A13 | Capacity board: discharge → bed dirty → cleaning → available; DBN updates | ❌ |
| A14 | LACE high-risk patient: suggested Charlson flags, clinician saves, follow-up task suggested | ❌ |
| A15 | Pending sign-up → admin approves → role and units apply within 30 s | ◑ API tests only |

Example (A9):
```js
test('unit scope with break-glass', async ({ page }) => {
  await signInAs(page, 'nurse', { units: ['4W'] });
  await page.goto(`/patients/${patientOn('5E')}`);
  await expect(page.getByText('outside your units')).toBeVisible();
  await page.getByRole('button', { name: 'Break glass' }).click();
  await page.getByLabel('Reason').fill('Covering 5E rapid response');
  await page.getByRole('button', { name: 'Open record' }).click();
  await expect(page.getByRole('heading', { level: 1 })).toContainText(patientName);
  // the audit trail shows BREAK_GLASS and PHI_READ for this user
});
```

### B. Interoperability (scripts against `sim`)

| # | Test | Pass criteria | Status |
|---|---|---|---|
| B1 | HL7 replay of a simulated day (~1,500 messages, mixed events and chaos above) | Every message ACKed. Duplicates replay the original ACK. Parked messages drain after their A01. Final census equals the replayed ADT state. | ◑ unit tests only |
| B2 | ADT ordering under concurrency: 5 parallel senders, same MRNs | No duplicate encounters; one active encounter per patient | ❌ |
| B3 | FHIR R4 conformance: run the official HL7 FHIR validator on every resource type returned | No errors (warnings logged) | ❌ |
| B4 | SMART launch against `launch.smarthealthit.org` (public, synthetic) | Patient/encounter context mapped, EHR lists displayed, no second sign-in | ❌ |
| B5 | Outbound delivery: Bundle validated by the FHIR validator; C-CDA schema check; receiver down → retries → DLQ/worklist | As stated | ◑ |

### C. Performance and capacity (Locust, against `sim`)

Locust fits the Python codebase. Each virtual user signs in through dev-login once, then follows the persona mix.

| Profile | Shape | What it answers |
|---|---|---|
| C1 Baseline | 50 users for 30 min | Normal latency, Neon CU usage |
| C2 Shift-change peak | Ramp 0 → 300 users in 5 min, hold 20 min | Login burst, census/plan reads, cold starts |
| C3 Discharge peak | 200 users at peak write mix, 1 h | Write contention, audit chain-head lock, task updates |
| C4 Soak | 120 users for 8 h (compressed day) | Memory/connection leaks, quota burn, error drift |
| C5 Spike | 50 → 400 in 30 s | Cold starts, Neon wake-up, 5xx under burst |
| C6 Notification fan-out | 250 open SSE streams | Function invocations per minute and event latency (target ≤ 5 s + poll) |

**Targets.** CRUD and AI latency come from the ADR-001 NFRs; the rest are proposed for this evaluation:

| Metric | Target |
|---|---|
| CRUD p95 | < 300 ms warm, < 1.5 s including cold start |
| CRUD p99 | < 1 s warm |
| Error rate | < 0.5% (excluding 429/403 by design) |
| AI summary (mock) | < 3 s |
| AI summary (Haiku) | p95 < 30 s, never above the 60 s function limit |
| Notification latency | p95 < 7 s |
| Audit chain after each run | `GET /api/audit/verify` valid, no sequence gaps |

Persona mix (requests per user-minute; tune after C1):
```python
class Nurse(HttpUser):
    wait_time = between(5, 20)
    def on_start(self):
        r = self.client.post("/api/auth/dev-login", json={"role": "nurse", "name": f"nurse-{uuid4().hex[:6]}"})
        self.client.headers["Authorization"] = f"Bearer {r.json()['token']}"
    @task(6)
    def my_tasks(self): self.client.get("/api/tasks/my", name="tasks/my")
    @task(3)
    def census(self): self.client.get("/api/census", name="census")
    @task(2)
    def complete_task(self):
        t = pick_open_task(self.client)                 # helper: a task assigned to this role
        if t: self.client.put(f"/api/tasks/{t}", json={"status": "completed"}, name="tasks/{id}")
    @task(1)
    def patient(self): self.client.get(f"/api/patients/{random_patient()}", name="patients/{id}")
```

**Efficiency measurements recorded per run:**
- p50, p95 and p99 latency per endpoint
- Vercel function invocations and GB-hours
- Neon CU-hours and active time
- cold-start count, and the latency of the first request after a Neon suspend
- requests per discharge, CU-hours per simulated day, and the projected monthly quota use

### D. Data integrity and concurrency (scripts + SQL checks after every C run)

| # | Check |
|---|---|
| D1 | 20 parallel "initiate discharge" calls for one encounter: exactly one workflow; the rest get 400 |
| D2 | Parallel completion of the same task by two nurses: one status change audited, no lost update |
| D3 | Audit chain valid; `seq` contiguous; `audit_chain_heads` matches the last row (SQL + `/api/audit/verify`) |
| D4 | Invariants by SQL: one active encounter per patient; one active workflow per encounter; no completed workflow with open tasks; every completed workflow has a signed summary or is `externally_discharged`; every `PHI_READ` entry has a user |
| D5 | Cross-tenant probe: create a second tenant, run its traffic in parallel, assert zero rows leak (RLS) |
| D6 | Encryption at rest: sampled `patients.name`/`diagnosis` and summary text start with `enc:v1:`; no plaintext names in any column (`pg_dump` scan for seeded names) |

### E. Security (against `sim`, plus read-only checks on production)

| # | Test | Status |
|---|---|---|
| E1 | Role × endpoint authorization matrix generated from `docs/api.md` (every route × every role → expected 2xx/403) | ◑ partial in pytest; build the full matrix |
| E2 | Unit-scope matrix with break-glass expiry (grant valid → expired → 403) | ◑ |
| E3 | OWASP ZAP baseline scan (passive) of `sim` and production | ❌ |
| E4 | Refresh-token reuse after the grace period revokes the session; idle-timeout revocation | ✅ unit; ❌ deployed |
| E5 | Log hygiene: export Vercel logs for a load run and grep for seeded names, MRNs, emails and dates | ❌ |
| E6 | Prompt injection: patient notes containing instructions; the assistant only produces proposals | ◑ |

### F. Resilience and operations

| # | Scenario | Expected |
|---|---|---|
| F1 | Neon auto-suspend: idle 10 min, then a burst | First request succeeds; record wake-up latency |
| F2 | Read-only downtime during C3 load, then backfill | Writes get 503 with `X-Read-Only`; reads continue; backfilled times inside the window; audit intact |
| F3 | LLM provider unavailable (`LLM_PROVIDER=none` or a bad key) | Summary returns a visible error; the workflow continues; provenance row written |
| F4 | Pager webhook down | Escalation recorded, page retried/logged, in-app notification still sent |
| F5 | Delivery receiver returns 500/timeout | Backoff, then failed state on the worklist; no duplicate sends |
| F6 | **Escalation cadence:** run escalations only via the daily cron for a day, then every 5 min from GitHub Actions | Measures how late a breached high-priority task is escalated (expected: up to 24 h on cron alone) |
| F7 | Bad deploy | `vercel rollback` (or promote the previous deployment) restores service; migrations remain compatible |
| F8 | Point-in-time restore of the Neon branch within 6 h | Data back; patients decrypt with the same `PHI_KEK` |

### G. AI quality in the simulated day

- Golden-set evals on Haiku (`python -m app.ai.evals --provider anthropic --model claude-haiku-5-5`).
- 50 generated summaries per run: grounding-warning rate, physician edit similarity (simulated edits are not meaningful; use the clinician session in H for real numbers), latency distribution, tokens per summary.
- Patient instructions: readability grade at or under target on all drafts. Translation spot-checked by a bilingual reviewer (no automated pass).

### H. Clinician usability walkthrough (manual, production or `sim`)

3–5 clinicians (nurse, physician, pharmacist, case manager), 60 min each, scripted tasks on synthetic patients.

Measure:
- task success
- time to complete key steps (initiate, complete a task, sign, find a patient)
- errors
- System Usability Scale score

Target: SUS ≥ 70, no critical task failure.

### I. Production monitoring (continuous, free)

- A GitHub Actions schedule runs `scripts/smoke_test.py https://dischargeflow.vercel.app` every 15 min and opens an issue on failure. ❌ (today it runs only after AWS deploys)
- A daily job calls `/api/audit/verify` with an admin service credential. ❌ Needs a non-interactive admin credential; decide whether that is acceptable, or keep it manual weekly.

## 5. What counts as "works in a real-time hospital" on this stack (exit criteria)

1. Every A-suite journey passes on `sim` with unit scoping on.
2. B1 replays a full simulated day with zero lost or duplicated ADT effects.
3. C2/C3 meet the latency and error targets. C4 shows no drift. The simulated day's Neon CU-hours fit the monthly quota.
4. D1–D6 hold after every load run.
5. No high findings in E1–E5.
6. F1–F8 behave as expected. F6 must show escalation within 15 min of breach, or the cadence is a blocking finding.
7. SUS ≥ 70 in the clinician walkthrough.

**Known blockers for a real deployment, whatever the results (not test failures):**
- no BAA on Vercel Hobby or Neon Free
- 6 h point-in-time recovery
- the Hobby non-commercial licence
- daily-only cron
- hospital SSO not live

## 6. Execution order and effort (one person)

| Step | Work | Effort |
|---|---|---|
| 1 | Create `sim` branch, Vercel env vars, seed script, dev-login per role/unit | 1 day |
| 2 | Point the existing Playwright specs at `sim`; add A6–A15 | 3–4 days |
| 3 | HL7 day generator + replay (B1, B2), FHIR validator run (B3), SMART sandbox (B4) | 2–3 days |
| 4 | Locust personas and profiles C1–C6, metrics capture | 2 days |
| 5 | Integrity SQL checks D1–D6 wired after each load run | 1 day |
| 6 | Security matrix E1/E2, ZAP, log grep | 1–2 days |
| 7 | Resilience drills F1–F8 | 1–2 days |
| 8 | AI runs (G) and clinician walkthrough (H) | 1 day + scheduling |
| 9 | Production monitoring (I) | 0.5 day |

**Status (2026-10-20):**
- **Built in `sim/`** ([README](../../sim/README.md)):
  - steps 1, 3 (B1 and B2) and 4 (C1–C6)
  - the existing Playwright specs, which now run against a deployment (`E2E_BASE_URL`)
- **Not built yet:**
  - the new browser journeys A6–A15
  - B3–B5
  - the SQL invariant checks (D4–D6); the audit chain (D3) is checked after every replay and load run

Run order per release: CI → `sim` journeys → B1 replay → C1 + C3 → D checks → production smoke.
Run C4/C5, F and H before any pilot decision.

## 7. Gaps in today's coverage (summary)

- **Deployed environments:** no automated test exercises a deployed environment beyond the unauthenticated smoke test, which today runs only in the AWS deploy workflow.
- **Load:** no load, soak or spike testing exists, and there are no latency baselines for Vercel + Neon.
- **Coverage:** Playwright covers 7 journeys. Phase 8–9 features (templates, delays, reconciliation, escalation, delivery, capacity, instructions, LACE) have API tests but no browser journeys.
- **External systems:** SMART and outbound delivery are tested against fakes only. FHIR output has not been run through the official validator.
- **Escalation timing:** on Vercel Hobby, escalation and delivery timing is daily, and nothing tests the delay a clinician would actually see.
- **Observability:** no automated check of PHI absence in production logs, and no recurring audit-chain verification.
