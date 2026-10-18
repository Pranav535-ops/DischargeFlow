# Deploy and roll back

## How a release flows

`.github/workflows/deploy.yml` runs on every push to `main` once `AWS_DEPLOY_ENABLED=true` is set:

1. **CI** (`ci.yml`): lint, tests on SQLite and PostgreSQL, AI evals, dependency audits, image scan, Terraform
   validation, frontend build.
2. **Build**: one API image, saved as a workflow artifact and tagged with the commit SHA.
3. **Staging**: the image is pushed to staging's ECR; `scripts/deploy/ecs_deploy.sh` registers a task definition
   revision, runs `python -m app.cli migrate` as a one-off task (the rollout stops if it fails), updates the
   service and waits for it to stabilise. The frontend is synced to S3, then `scripts/smoke_test.py` runs.
4. **Prod**: waits for a required reviewer on the `prod` GitHub environment, then repeats step 3 with the same
   image bytes.

## Rules that keep rollback safe

- **Migrations must work with the previous release.** Add columns as nullable or with defaults, and remove or rename
  in a later release (expand, then contract). Rolling the code back never rolls the schema back.
- Never edit a published migration. `tests/test_platform.py` fails if models and migrations drift.

## Automatic rollback

The ECS deployment circuit breaker replaces a release whose tasks fail health checks with the previous task
definition. The deploy job then fails with "the circuit breaker rolled the deployment back". Check the service events
and the stopped tasks' reasons.

## Manual rollback

1. Find the last good revision: ECS console → cluster `dischargeflow-<env>` → service `api` → Deployments, or
   `aws ecs list-task-definitions --family-prefix dischargeflow-<env>-api --sort DESC --max-items 5`.
2. Roll back:

   ```bash
   aws ecs update-service --cluster dischargeflow-<env> --service api --task-definition dischargeflow-<env>-api:<revision>
   aws ecs wait services-stable --cluster dischargeflow-<env> --services api
   ```

3. Frontend: S3 versioning is on. Re-run the deploy workflow for the last good commit (`workflow_dispatch` on that
   SHA), or restore the previous `index.html` version and invalidate `/index.html` in CloudFront.
4. Run `python3 scripts/smoke_test.py https://<app-domain>`.
5. Revert the offending commit on `main` so the next deploy doesn't re-release it.

## A migration failed mid-deploy

The service still runs the previous release (migrate runs before the rollout). Alembic runs each migration in a
transaction on PostgreSQL, so a failed migration leaves the schema at the previous revision. Read the migrate task's
log (stream prefix `api/`), fix forward in a new commit, and redeploy.
