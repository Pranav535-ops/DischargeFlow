# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

DischargeFlow is a hospital discharge management system: a FastAPI backend (`backend/app/`) on SQLAlchemy + PostgreSQL, and a CRA/craco React 19 frontend (`frontend/`). It was scaffolded on the Emergent platform (`.emergent/`, Emergent Auth, Emergent LLM integrations). Product requirements are in `memory/PRD.md`, and the 12-week roadmap is in `USER_STORIES_12_WEEKS.md`. **The target architecture and phased plan are in `docs/architecture/ADR-001-production-architecture.md`.** Phase 1 (modular backend + Postgres + Alembic + tests) is done; later phases (Cognito, Bedrock AI layer, Terraform/ECS) are not started.

`project_implementation_plan.md` predates the repo audit (it assumes Node and microservices) and is superseded by ADR-001. `project_overview.md` describes a different project (SkillGraph V2). Ignore both.

## Commands

Backend (run from `backend/`, with `venv` active):
```bash
pip install -r requirements.txt
alembic upgrade head                 # create/upgrade the schema (required before first run)
python -m app.cli seed               # idempotent demo patients; refuses in production without --force
python -m uvicorn server:app --port 8000 --reload
python -m pytest                     # full suite, in-memory SQLite
python -m pytest tests/test_discharge_workflow.py::test_full_discharge_happy_path   # single test
alembic revision --autogenerate -m "describe change"   # after editing models; review the generated file
```
Run the suite against Postgres by setting `TEST_DATABASE_URL=postgresql+asyncpg://...`. CI runs both SQLite and a Postgres 16 service. `tests/test_platform.py` shells out to Alembic to check `upgrade` → `check` (model/migration drift) → `downgrade`, so a model change without a migration fails the tests.

Frontend (from `frontend/`): `npm start` (port 3000), `npm run build`, `npm test` (no frontend tests yet). Set `REACT_APP_BACKEND_URL=http://localhost:8000` for local dev; if it is empty, the frontend calls a relative `/api`.

Full stack: `docker compose up --build` starts Postgres, runs a one-shot `migrate` service, then the backend (with `SEED_ON_STARTUP=true`) and the frontend.

## Configuration (`app/core/config.py`)
`APP_ENV` (`development` | `test` | `production`; anything else counts as production), `DATABASE_URL`, `JWT_SECRET`, `CORS_ORIGINS` (comma-separated), `SEED_ON_STARTUP`, `EMERGENT_LLM_KEY`. In production, missing `DATABASE_URL` or `JWT_SECRET` makes startup fail. In dev they default to `backend/dischargeflow.db` (SQLite) and an ephemeral random secret, so sessions die on restart. `normalize_database_url` accepts provider URLs (`postgres://…?sslmode=require`) and converts them for asyncpg.

## Backend architecture

`server.py` is only a shim (`from app.main import app`) so that uvicorn, the Dockerfile and `vercel.json` keep working. `app/main.py:create_app` wires the middleware and mounts each module's router under `/api`.

Modular monolith: each package in `app/` owns its tables (`models.py`) and HTTP surface (`router.py`), plus `service.py` where logic is shared. `app/models.py` imports every model so `Base.metadata` is complete for Alembic and tests; **a new model must be added there.** Modules: `identity` (users, Emergent OAuth session exchange), `patients` (incl. consent/erasure), `discharge` (workflows + tasks), `ai` (summaries), `notifications`, `analytics`, `insurance`, `chatbot`, `audit`.

Key conventions:
- **One transaction per request.** Routes take `db: DB` (`app/core/db.py`), a dependency declared with `scope="function"`, so it commits *before* the response is sent and rolls back everything on any exception. Services `flush()` instead of committing. Audit rows (`audit.service.write_audit`) and notifications go into the same transaction as the change they describe.
- **The discharge state machine lives in `discharge/service.py`.** `sync_workflow_status` moves a workflow `in_progress ⇄ pending_approval` based on open tasks; call it after any task change (task routes, summary approval and the chatbot all do). `complete_discharge` requires zero open tasks and an approved summary. The database enforces one active workflow per patient with a partial unique index (`uq_workflows_one_active_per_patient`) and one summary per workflow with a unique constraint.
- IDs are prefixed strings (`pat_…`, `wf_…`, `task_…`), kept from the Mongo era so the API contract didn't change. `Base.to_dict()` serializes rows with ISO datetimes, which is the shape the frontend expects. `UTCDateTime` keeps timestamps timezone-aware on SQLite as well. SQLite runs with `PRAGMA foreign_keys=ON`.
- Tasks may have `workflow_id = NULL` (ad-hoc tasks with no active discharge). `priority` is an int: 1 high, 2 medium, 3 low.
- `PatientUpdate` whitelists editable fields and silently ignores any others.
- **Auth:** a stateless HS256 JWT (7 days) carries the user and their role. `get_current_user` reads the `session_token` cookie or a Bearer header. `require_roles([...])` is the RBAC dependency. Because the role is in the token, a role change only takes effect at the next login (fixed in ADR Phase 2). Login: frontend → `auth.emergentagent.com` → `#session_id=` in the URL hash → `POST /api/auth/session`, which upserts the user and returns the token as a cookie and in the body. The frontend stores it in `localStorage`. `AuthContext.login()` warns not to hard-code the redirect URL.
- **AI:** `ai/service.generate_summary_text` is the only LLM call site. It lazily imports `emergentintegrations` (not in `requirements.txt`) and returns 503 if that or `EMERGENT_LLM_KEY` is missing. Tests monkeypatch it.
- The chatbot (`chatbot/router.py`) matches keywords. The order of `INTENTS` matters, because the first matching phrase wins.

## Frontend architecture
- `App.js` routes only `/login` and `/dashboard`. If the URL hash contains `session_id=`, `AuthCallback` renders before routing runs.
- `pages/Dashboard.js` is a single-page shell that switches between `components/*View.js` with `activeView` state. A new screen is a view component wired into `Dashboard` and `Sidebar`.
- Every component builds its own `` const API = `${process.env.REACT_APP_BACKEND_URL || ""}/api` `` and calls axios directly; there is no shared client yet.
- UI: shadcn/ui (Radix) components in `components/ui/`, Tailwind, a forced dark theme, lucide icons and Recharts. The `@/` alias points to `src/`. Design tokens are in `design_guidelines.json`.

## Deployment notes
- `vercel.json` serves `backend/server.py` as a Python function. The backend now needs a real hosted Postgres (`DATABASE_URL`, e.g. Neon or RDS) plus `APP_ENV=production` and `JWT_SECRET`. On Vercel, `make_engine` uses `NullPool`. Run `alembic upgrade head` against that database before deploying.
- `k8s/` has a Postgres StatefulSet, a `backend-migrate` Job, and a backend Deployment that reads a `backend-secrets` Secret (see the comments in each file).
