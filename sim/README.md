# Hospital simulation

Tools for the end-to-end test plan in [`docs/testing/hospital-simulation-test-plan.md`](../docs/testing/hospital-simulation-test-plan.md), steps 1–4:

- a production-like **sim** deployment
- a synthetic hospital **seed**
- the Playwright journeys run **against that deployment**
- a replayed **HL7 ADT day**
- **Locust** load profiles

Everything is synthetic, and the tools use the API only, never the database.

## 1. The sim environment (once)

`sim` is a long-lived git branch that is identical to `main`. Vercel builds it as a preview deployment, and the Neon integration gives it its own database branch, `preview/sim`. That branch starts as a copy of production's data, which is synthetic.

1. Create the branch and push it:
   ```bash
   git push origin main:sim
   ```
2. In Vercel → Project → Settings → Environment Variables, add these for **Preview**, limited to the git branch **`sim`**:

   | Variable | Value | Why |
   |---|---|---|
   | `APP_ENV` | `test` | Allows the dev login; still requires a real `JWT_SECRET` and keeps every other production check |
   | `ENABLE_DEV_LOGIN` | `true` | The simulation signs in as many synthetic staff |
   | `LLM_PROVIDER` | `mock` | Offline AI, no cost; use `anthropic` and a key for AI runs (plan §4.G) |
   | `UNIT_SCOPED_ACCESS` | `true` | Staff see only their units, as on a real ward |
   | `JWT_SECRET` | your own value | Paste it yourself; see below |

   - **`JWT_SECRET`:** generate one and paste it yourself. Don't reuse production's. Serverless instances must share it, which is why the ephemeral development secret won't work here.
   - **`PHI_KEK`:** leave it as the Preview value it already has. The copied data is encrypted with it.
3. Redeploy the `sim` branch, then check that `https://<sim deployment>/api/health` reports `postgresql`.
4. Before each run, bring `sim` up to `main`. Only environment variables may differ, so results carry over to production:
   ```bash
   git push origin main:sim --force-with-lease
   ```

The `sim` database branch counts toward Neon's 10 branches. The cleanup workflow only deletes branches whose pull request closed, so `preview/sim` stays.

Install the tools in their own virtualenv:
```bash
python -m venv sim/.venv
```
```bash
sim/.venv/Scripts/pip install -r sim/requirements.txt
```

## 2. Seed the hospital

```bash
cd sim && .venv/Scripts/python seed.py --base-url https://<sim deployment>
```

The seed creates:
- 24 units (`3W` … `8S`)
- 175 staff with unit memberships (120 nurses, 40 physicians, 12 pharmacists, 3 admins)
- an ADT integration client
- about 390 patients admitted through HL7 A01 (18 beds × 24 units × 90% occupancy)
- a discharge plan (expected date and disposition) for each stay

Seeding is idempotent. The integration key and the staff list are kept in `sim/.state.json`, which is gitignored and is the only copy of the key. Delete it and revoke the client on the admin screen to start over.

## 3. Playwright against the sim

```bash
cd frontend && E2E_BASE_URL=https://<sim deployment> npx playwright test
```

Nothing is started locally. The SMART launch spec is skipped, because its fake EHR runs on this machine.

Don't run `downtime.spec.js` during a load test: it puts the whole tenant into read-only mode for a moment.

## 4. A simulated ADT day

```bash
cd sim && .venv/Scripts/python adt_day.py --base-url https://<sim deployment> --duration 600
```

This compresses 24 simulated hours into 10 minutes, about 400 messages:
- discharges, admissions, transfers and updates
- cancellations (A11/A13)
- A03s sent before their A01
- duplicates
- A40 merges

Afterwards it checks that:
- every ACK was the expected one
- each duplicate got its original ACK
- nothing is left parked
- the census matches what the feed implies
- the audit chain verifies

Add `--unordered` to drop per-patient ordering (test plan B2), or `--dry-run` to only write the messages to `sim/out/`. A JSON report lands in `sim/out/`.

## 5. Load profiles

```bash
cd sim && DF_PROFILE=shift_change .venv/Scripts/locust -f locustfile.py --host https://<sim deployment> --headless --csv out/shift --html out/shift.html
```

| `DF_PROFILE` | Shape | Plan |
|---|---|---|
| `baseline` | 50 users, 30 min | C1 |
| `shift_change` | ramp to 300 in 5 min, hold 20 min | C2 |
| `discharge_peak` | 200 users, 1 h, write-heavy | C3 |
| `soak` | 120 users, 8 h | C4 |
| `spike` | 50 → 400 in 30 s, back to 50 | C5 |

Add `DF_SSE_USERS=250` to hold that many notification streams open on top of the profile (C6).

- **How refusals are counted:** expected refusals (409 open prerequisite, 400 delay code needed, 403 outside your units) are recorded as `refused` entries, so the failure rate counts only real errors.
- **Audit check:** after the run, the audit chain is verified, and the exit code is 1 if it is broken.

Record Neon CU-hours and Vercel function usage before and after each run (test plan §4.C). The free plans' monthly quotas are the limit you'll hit first.

## Try it locally first

The same tools work against a local backend:
```bash
cd backend && APP_ENV=development ENABLE_DEV_LOGIN=true UNIT_SCOPED_ACCESS=true python -m uvicorn server:app --port 8000
```
```bash
cd sim && .venv/Scripts/python seed.py --base-url http://localhost:8000 --beds-per-unit 4
```

SQLite runs request transactions one at a time and has no row-level security, so use it only to check the scripts, never for performance numbers.
