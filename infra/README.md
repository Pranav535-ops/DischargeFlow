# Infrastructure (AWS, Terraform)

ADR-001 Phase 4. Two environments, `envs/staging` and `envs/prod`, each a complete stack from `modules/stack`:

| Module | What it creates |
|---|---|
| `network` | VPC across 2 AZs, public and private subnets, NAT (one in staging, one per AZ in prod), rejected-traffic flow logs |
| `database` | RDS PostgreSQL 16, KMS-encrypted, TLS-only, PITR (7 / 35 days), Performance Insights, RDS-managed master password |
| `app` | ECR, ECS Fargate cluster and API service (circuit-breaker rollback, CPU autoscaling), HTTPS ALB reachable only from CloudFront, Secrets Manager entries, IAM roles, optional OpenTelemetry collector sidecar sending to X-Ray |
| `edge` | CloudFront serving the React build from private S3 and `/api/*` from the ALB (one origin), AWS WAF (rate limit + AWS managed rules), ACM certificates, Route 53 records |
| `observability` | SNS email alarms (5xx, p95 latency, unhealthy targets, RDS CPU and storage, ECS memory, application ERROR logs) and a CloudWatch dashboard |
| `github_oidc` | The deploy role GitHub Actions assumes via OIDC, limited to this repo's matching GitHub environment |

CI runs `terraform fmt -check` and `terraform validate` on every push. Nothing here has been applied yet: the project
has no AWS account, and production currently runs on Vercel + Neon.

## Before the first apply

1. **An AWS account with a signed BAA** (AWS Artifact) before any real patient data goes in.
2. **A domain in Route 53.** Note its hosted zone id. The stacks create `app.<domain>` / `api.<domain>` (prod) and
   `app.staging.<domain>` / `api.staging.<domain>`.
3. **State storage**, once per account: an S3 bucket (versioned, encrypted, public access blocked) and a DynamoDB
   table with partition key `LockID` (string). Copy `envs/<env>/backend.hcl.example` to `backend.hcl` and fill it in.
4. Copy `envs/<env>/terraform.tfvars.example` to `terraform.tfvars` and fill it in. Both files are gitignored.

## Apply (staging first: it creates the account-wide GitHub OIDC provider)

```bash
cd infra/envs/staging
terraform init -backend-config=backend.hcl
terraform plan -out tfplan
terraform apply tfplan
```

Then, before the API can start:

1. **Set the secrets** (they are created empty; their values never enter Terraform state). See
   `docs/runbooks/rotate-secrets.md` for generating them, and keep an offline backup of `phi_kek`:
   ```bash
   aws secretsmanager put-secret-value --secret-id dischargeflow-staging/jwt_secret --secret-string "..."
   aws secretsmanager put-secret-value --secret-id dischargeflow-staging/phi_kek --secret-string "..."
   ```
2. **Confirm the alarm subscription** emails from SNS.
3. **GitHub**: create environments `staging` and `prod` (Settings → Environments), add required reviewers to `prod`,
   and copy `terraform output github_environment_variables` into each environment's variables. Finally set the
   repository variable `AWS_DEPLOY_ENABLED=true`; until then `deploy.yml` skips every job.
4. Push to `main` (or run the Deploy workflow manually). The first run migrates the empty database and deploys.

The task definition starts with an image tag `bootstrap` that does not exist, so the service has no healthy tasks
until the first deploy registers a real image. That is expected.

## Moving production from Vercel/Neon

1. Freeze writes (maintenance window), then `pg_dump --format=custom` from Neon.
2. `pg_restore` into RDS from a one-off ECS task or a bastion, then run `python -m app.cli migrate`.
3. **Copy `PHI_KEK` exactly** from Vercel into `dischargeflow-prod/phi_kek`; patient identifiers are unreadable
   without it. Verify a few patients decrypt before switching DNS.
4. Point users at `app.<domain>`. Existing sessions do not carry over (new origin), so users sign in again.

## Choices worth knowing

- **Cognito, SQS/worker and ElastiCache are not here yet.** Login stays on Emergent's Google OAuth (deferred in Phase 2),
  AI generation is synchronous (item 11 deferred), and permission/identity caches are per process. CloudFront's
  origin read timeout is 60 s for that reason.
- **`llm_provider` defaults to `none`.** `bedrock` keeps prompts inside the AWS account under the BAA; enable it after
  requesting model access in the Bedrock console. Verify the task role's `bedrock:*` actions against the endpoint
  the SDK client uses before relying on it.
- **The OpenTelemetry collector image** defaults to `latest`; pin it to a tested version before production.
- **Costs** depend on region and usage; check the AWS Pricing Calculator before applying. The largest fixed costs are
  NAT gateways, the multi-AZ database and the load balancer.

## Live notifications (ADR-002 item 21)

The ECS task definition sets `NOTIFICATION_TRANSPORT=listen` and `NOTIFICATION_STREAM_SECONDS=300`. Each API task
holds one extra database connection with `LISTEN notifications` and pushes notification ids to its open SSE streams as
the inserting transaction commits (trigger from migration 0016), instead of every stream polling every 5 s. Size RDS
`max_connections` for one more connection per task. `DB_HOST` must be RDS itself: if an RDS Proxy or PgBouncer in
transaction mode is ever put in front, set `DATABASE_DIRECT_URL` to a direct endpoint, or the stream falls back to
polling. Streams send a heartbeat every 15 s, which keeps them open through the ALB's default 60 s idle timeout.
