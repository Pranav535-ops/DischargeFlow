# Runbooks

Operational procedures for the AWS deployment (`infra/`, ADR-001 Phase 4). Each alarm in
`infra/modules/observability` points here.

| Situation | Runbook |
|---|---|
| Deploying, rolling back, a failed deploy | [deploy-and-rollback.md](deploy-and-rollback.md) |
| Data loss or corruption; restoring to a point in time | [restore-database.md](restore-database.md) |
| Rotating `JWT_SECRET`, `PHI_KEK`, the database password or an API key; a suspected secret leak | [rotate-secrets.md](rotate-secrets.md) |
| AI summaries failing, slow, or producing suspect output | [ai-incident.md](ai-incident.md) |
| Planned maintenance or an outage: staff work on paper, then back-enter it | [downtime.md](downtime.md) |

## First steps for any alarm

1. Open the environment's CloudWatch dashboard (`dischargeflow-<env>`): requests, errors, latency, ECS and RDS.
2. Find the failing requests in CloudWatch Logs Insights on `/ecs/dischargeflow-<env>/api`:

   ```
   fields @timestamp, level, route, status, duration_ms, request_id, msg
   | filter level = "ERROR" or status >= 500
   | sort @timestamp desc
   | limit 50
   ```

   Every log line and response carries `request_id`, so one id from a user's error report or the browser's
   network tab (`X-Request-ID` header) finds the full trace: `filter request_id = "<id>"`. With tracing on,
   the same request appears in X-Ray.
3. `api-unhealthy-targets` almost always means the database: check RDS status and connections first.
4. Logs are PHI-scrubbed but are still operational data under the HIPAA minimum-necessary rule. Do not export
   them outside the AWS account, and never paste patient details into tickets or chat.

## Alarm reference

| Alarm | Usual cause | First action |
|---|---|---|
| `api-5xx` | Bad deploy, database outage, provider error surfacing as 5xx | If it started with a deploy, roll back ([deploy-and-rollback.md](deploy-and-rollback.md)) |
| `api-p95-latency` | Slow queries, CPU saturation, AI calls (synchronous until the worker exists) | Dashboard "Slowest routes" widget; RDS Performance Insights |
| `api-unhealthy-targets` | Database unreachable, crash loop after deploy | ECS service events; task stopped reasons |
| `db-cpu`, `db-free-storage` | Load growth, runaway query, storage at its autoscaling maximum | Performance Insights; raise `max_allocated_storage` |
| `ecs-memory` | Leak or undersized tasks | Raise `memory` in the app module; look for growth across deploys |
| `app-errors` | Exceptions logged at ERROR | Logs Insights query above |
