# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

DischargeFlow is a hospital discharge management system: a FastAPI backend (`backend/app/`) on SQLAlchemy + PostgreSQL, and a CRA/craco React 19 frontend (`frontend/`). It was scaffolded on the Emergent platform (`.emergent/`, Emergent Auth). Product requirements are in `memory/PRD.md`, and the 12-week roadmap is in `USER_STORIES_12_WEEKS.md`. **The target architecture and phased plan are in `docs/architecture/ADR-001-production-architecture.md`.** Phase 1 (modular backend + Postgres + Alembic + tests) and Phase 2 (hardened sessions, DB-backed permissions, tenant RLS, PHI encryption, tamper-evident audit) are done. Phase 2 kept the Emergent Google login rather than switching to Cognito. Phase 3 (governed AI layer: LLM gateway, de-identification, structured summaries with provenance, golden-set evals, tool-calling assistant) is done except the async outbox/SQS worker (item 11). Phase 4 (Terraform for AWS, deploy pipeline, observability, runbooks) is written and validated but not applied: there is no AWS account, so production still runs on Vercel + Neon. Phase 5 (frontend, FHIR) is not started.

`project_implementation_plan.md` predates the repo audit (it assumes Node and microservices) and is superseded by ADR-001. `project_overview.md` describes a different project (SkillGraph V2). Ignore both.

## Commands

Backend (run from `backend/`, with `venv` active):
```bash
pip install -r requirements.txt
python -m app.cli migrate            # alembic upgrade head + encrypt any plaintext PHI (use this, not bare alembic)
python -m app.cli seed               # idempotent demo patients; refuses in production without --force
python -m uvicorn server:app --port 8000 --reload
python -m pytest                     # full suite, in-memory SQLite
ruff check .                         # lint gate in CI (errors only, see ruff.toml)
python -m app.cli rewrap-deks        # PHI KEK rotation step, see docs/runbooks/rotate-secrets.md
python -m app.ai.evals               # golden-set AI evals on the mock adapter (CI gate); --provider anthropic --model claude-haiku-5-5 for a real model
python -m pytest tests/test_discharge_workflow.py::test_full_discharge_happy_path   # single test
alembic revision --autogenerate -m "describe change"   # after editing models; review the generated file
```
Run the suite against Postgres by setting `TEST_DATABASE_URL=postgresql+asyncpg://...`. CI runs both SQLite and a Postgres 16 service. Tenant-isolation (RLS) tests are marked `requires_postgres` and only run against Postgres. `tests/test_platform.py` shells out to Alembic to check `upgrade` → `check` (model/migration drift) → `downgrade`, and upgrades real 0001-era data through `app.cli migrate`. A model change without a migration therefore fails the tests.

Frontend (from `frontend/`): `npm start` (port 3000), `npm run build`, `npm test` (no frontend tests yet). Set `REACT_APP_BACKEND_URL=http://localhost:8000` for local dev; if it is empty, the frontend calls a relative `/api`.

Full stack: `docker compose up --build` starts Postgres, runs a one-shot `migrate` service, then the backend (with `SEED_ON_STARTUP=true`) and the frontend.

## Configuration (`app/core/config.py`)
`APP_ENV` (`development` | `test` | `production`; anything else counts as production), `DATABASE_URL`, `PHI_KEK` (base64 of 32 bytes), `JWT_SECRET`, `CORS_ORIGINS` (comma-separated), `DEFAULT_TENANT_ID` (default `default`), `AUTO_MIGRATE`, `SEED_ON_STARTUP`, `LLM_PROVIDER` (`mock` | `anthropic` | `bedrock` | `none`; defaults to `mock` in development/test and `none` elsewhere), `ANTHROPIC_API_KEY`, `AWS_REGION` (Bedrock), `LLM_SUMMARY_MODEL` (default `claude-sonnet-5-5`), `LLM_ASSISTANT_MODEL` (default `claude-haiku-5-5`), `LOG_FORMAT` (`json` default in production, `text` in dev), `LOG_LEVEL`, `PHI_KEK_PREVIOUS` (comma-separated retired KEKs during rotation), `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` (used to build the URL when `DATABASE_URL` is unset, as on ECS), `OTEL_EXPORTER_OTLP_ENDPOINT` (turns on tracing if `requirements-otel.txt` is installed).
- In production, a missing `DATABASE_URL` or `PHI_KEK` fails at settings load. A missing `JWT_SECRET` fails in `create_app()` only, so migrations and the CLI run without it.
- In dev these default to `backend/dischargeflow.db` (SQLite), a fixed development-only KEK, and an ephemeral JWT secret (sessions die on restart).
- `normalize_database_url` accepts provider URLs (`postgres://…?sslmode=require`) and converts them for asyncpg. `-pooler` hosts get PgBouncer-safe settings.
- `AUTO_MIGRATE=true` (set on Vercel) migrates on the first request per process, under a Postgres advisory lock (`core/migrate.py`). It exists because Vercel has no release step and every preview gets a fresh Neon branch. Elsewhere, run `app.cli migrate` as a one-off job.

## Backend architecture

`server.py` is only a shim (`from app.main import app`) so that uvicorn, the Dockerfile and `vercel.json` keep working. `app/main.py:create_app` wires the middleware and mounts each module's router under `/api`.

Modular monolith: each package in `app/` owns its tables (`models.py`) and HTTP surface (`router.py`), plus `service.py` where logic is shared. `app/models.py` imports every model so `Base.metadata` is complete for Alembic and tests; **a new model must be added there.** Modules: `identity` (users, Emergent OAuth session exchange), `patients` (incl. consent/erasure), `discharge` (workflows + tasks), `ai` (LLM gateway, prompts, de-identification, summaries, evals), `notifications`, `analytics`, `insurance`, `chatbot` (tool-calling assistant), `audit`.

Key conventions:
- **One transaction per request.** Routes take `db: DB` (`app/core/db.py`), a dependency declared with `scope="function"`, so it commits *before* the response is sent and rolls back everything on any exception. Services `flush()` instead of committing. Audit rows (`audit.service.write_audit`) and notifications go into the same transaction as the change they describe.
- **The discharge state machine lives in `discharge/service.py`.** `sync_workflow_status` moves a workflow `in_progress ⇄ pending_approval` based on open tasks; call it after any task change (task routes, summary approval and the chatbot all do). `complete_discharge` requires zero open tasks and an approved summary. The database enforces one active workflow per patient with a partial unique index (`uq_workflows_one_active_per_patient`) and one summary per workflow with a unique constraint.
- IDs are prefixed strings (`pat_…`, `wf_…`, `task_…`), kept from the Mongo era so the API contract didn't change. `Base.to_dict()` serializes rows with ISO datetimes, which is the shape the frontend expects. `UTCDateTime` keeps timestamps timezone-aware on SQLite as well. SQLite runs with `PRAGMA foreign_keys=ON`.
- Tasks may have `workflow_id = NULL` (ad-hoc tasks with no active discharge). `priority` is an int: 1 high, 2 medium, 3 low.
- `PatientUpdate` whitelists editable fields and silently ignores any others.
- **Auth:**
  - Access tokens are 15-minute HS256 JWTs (`sub`, `sid`, `tid`, `typ=access`), sent only as a Bearer header.
  - `get_current_user` loads the role from the database and checks the `auth_sessions` row is still active, cached 30 s per process. Role changes and logout therefore apply within seconds. Call `invalidate_identity()` after changing either.
  - The refresh token lives in the `df_refresh` cookie (httpOnly, SameSite=Strict, `Path=/api/auth`) as `tenant.session.secret`; only its SHA-256 is stored. It rotates on every `/api/auth/refresh`. Reuse of the previous token after a 30 s grace period revokes the session (`identity/sessions.py`).
  - Login: frontend → `auth.emergentagent.com` → `#session_id=` in the URL hash → `POST /api/auth/session`. New users join `DEFAULT_TENANT_ID` as `nurse`.
  - The frontend keeps the access token in memory only (`AuthContext.js`), refreshes on load and before expiry, and retries once on a 401. `AuthContext.login()` warns not to hard-code the redirect URL.
- **Permissions:** endpoints use `Depends(requires("<permission>"))`. The `role_permissions` table maps roles to permissions (seeded by migration 0002 from `identity/permissions.py:DEFAULT_ROLE_PERMISSIONS`; admin holds all). A new permission needs a migration that inserts its rows.
- **Tenancy:**
  - Every tenant-scoped model uses `TenantMixin` (`tenant_id` defaults from the `current_tenant_id` contextvar and raises if unset).
  - On Postgres, `use_tenant(db, tenant_id)` runs `SET LOCAL ROLE dischargeflow_app` and sets `app.tenant_id`. RLS policies (`core/rls.py`, frozen copy in migration 0002) then filter and check every row. Auth does this automatically.
  - Unauthenticated code paths (login, refresh, seed, CLI) must call `use_tenant` themselves.
  - A new tenant-scoped table needs `TenantMixin`, an entry in `rls.TENANT_TABLES`, and a migration adding its policy and grants. A test compares migrated policies with `TENANT_TABLES`.
  - SQLite has no RLS, so local SQLite is effectively single-tenant.
- **PHI:**
  - `Patient.name`, `date_of_birth` and `diagnosis` are synonyms over ciphertext columns: AES-256-GCM with a per-patient data key wrapped by `PHI_KEK`, with the patient id and field as associated data (`core/crypto.py`).
  - They can't be filtered or sorted in SQL; load and filter in Python, as the chatbot's patient lookup does.
  - `Patient.shred()` (erasure) destroys the key and overwrites the fields.
  - Never log or return `phi_dek`. `__api_exclude__` keeps it out of `to_dict()`.
- **Audit:**
  - `audit.service.write_audit` locks the tenant's `audit_chain_heads` row and appends an entry whose SHA-256 covers its fields plus the previous hash. `GET /api/audit/verify` recomputes the chain.
  - `audit_logs` is append-only: triggers block UPDATE and DELETE for every role, and the app role only has INSERT and SELECT. Never write code that edits audit rows.
- **AI gateway (`ai/llm.py`):** every model call goes through `get_llm_client(purpose)` and the `LLMClient` interface. Adapters: `ClaudeClient` (Anthropic API or Bedrock, same SDK) and `MockLLMClient` (deterministic, offline; only restates facts it is given). Never import a provider SDK elsewhere. With `LLM_PROVIDER=none` summaries return 503; the assistant falls back to the mock. A refusal or provider error raises `LLMError` (no silent model fallback, so provenance names the real model).
- **Prompts (`ai/prompts/<name>@<version>.md`):** `ACTIVE` picks the version. Never edit a published version: add the next one, point `ACTIVE` at it and pin its hash in `tests/test_ai.py:PROMPT_HASHES`.
- **De-identification (`ai/deid.py`):** text sent to a model has names replaced by tokens (`[PATIENT_1]`), contact details and dates scrubbed, and only the fields the prompt needs (`ai/service.summary_facts`: age, length of stay, diagnosis, task titles, physician notes). Output is re-identified. Names shared by two people become `[AMBIGUOUS_NAME]`.
- **Summaries (`ai/service.py`):** structured JSON validated by `ai/schemas.DischargeSummaryDoc`, one retry on invalid output, then a visible 502. Grounding warnings (`ai/grounding.py`: meds/diagnoses not in the input) are stored with the draft. Every call adds an `ai_generations` provenance row (model, prompt version, input hash, tokens, latency; never prompt/output text). A failed generation returns a `JSONResponse` instead of raising so its provenance and audit rows still commit. Approval accepts the physician's edited `content` and stores `original_content`, `edit_diff` and `edit_similarity`.
- **Evals (`ai/evals/`):** `golden_set.json` holds synthetic patients with expected and forbidden facts; `python -m app.ai.evals` runs them through `run_summary_model` (the production path) and exits non-zero on any hallucination, PHI leak, failure, or coverage below the threshold. A prompt or model change must keep it green.
- **Assistant (`chatbot/`):** an LLM tool-calling loop over `chatbot/tools.py`, which wraps the same service functions as the HTTP API (`discharge.service.create_task`/`update_task`/`initiate_discharge`, `insurance.service.create_claim`). Each tool declares the permission its endpoint requires (checked with `security.ensure_permission`) and every call is audited. Write tools never run from the model: they create an `assistant_actions` proposal that the same user confirms (`POST /api/chatbot/actions/{id}/confirm`, single use, 10-minute expiry, permission re-checked). Tool results are de-identified and wrapped in `<tool_data>` as untrusted data. A new tool needs an args model, its HTTP permission, and a test.

- **Logging (`core/observability.py`):** never log patient fields, request bodies, prompts or model output. Log lines are JSON in production with `request_id`, `tenant_id` and `user_id` attached automatically, and a PHI scrubber redacts emails, phones, SSNs, MRNs and dates as a backstop. Pass structured fields with `extra={...}`.

## Frontend architecture
- `App.js` routes only `/login` and `/dashboard`. If the URL hash contains `session_id=`, `AuthCallback` renders before routing runs.
- `pages/Dashboard.js` is a single-page shell that switches between `components/*View.js` with `activeView` state. A new screen is a view component wired into `Dashboard` and `Sidebar`.
- Every component builds its own `` const API = `${process.env.REACT_APP_BACKEND_URL || ""}/api` `` and calls axios directly; there is no shared client yet.
- UI: shadcn/ui (Radix) components in `components/ui/`, Tailwind, a forced dark theme, lucide icons and Recharts. The `@/` alias points to `src/`. Design tokens are in `design_guidelines.json`.

## Deployment notes
- AWS (not yet applied): `infra/` (see `infra/README.md`), `.github/workflows/deploy.yml` (dormant until the `AWS_DEPLOY_ENABLED` repository variable is `true`), `scripts/deploy/ecs_deploy.sh`, `scripts/smoke_test.py`, and runbooks in `docs/runbooks/`. Migrations must stay compatible with the previous release, because the deploy migrates before rolling out.
- `vercel.json` serves `backend/server.py` as a Python function. `server.py` puts `backend/` on `sys.path` because Vercel imports it by path from the repo root.
- Production runs on Neon Postgres via the Vercel integration, which injects `DATABASE_URL`. Preview deployments get their own Neon branch.
- Vercel env: `APP_ENV=production`, `JWT_SECRET`, `PHI_KEK`, `CORS_ORIGINS`, `AUTO_MIGRATE=true`. On Vercel, `make_engine` uses `NullPool`.
- `k8s/` has a Postgres StatefulSet, a `backend-migrate` Job, and a backend Deployment that reads a `backend-secrets` Secret (see the comments in each file).
