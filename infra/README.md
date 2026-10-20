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
| `cognito` | Hospital SSO (ADR-002 item 2): user pool (MFA required for local accounts, no self sign-up), SAML and/or OIDC federation with the hospital directory (groups into `custom:groups`), hosted-UI domain, the login page's public PKCE app client |

The `app` module also creates the KMS key that wraps patient data keys (`alias/dischargeflow-<env>-phi`, ADR-002
item 5) and sets `PHI_KEY_PROVIDER=kms`, the Cognito `OIDC_*` variables and `EMERGENT_LOGIN=false` in the task;
both environments set `LLM_PROVIDER=bedrock`.

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

- **SQS/worker and ElastiCache are not here yet.** AI generation is synchronous (ADR-001 item 11 deferred), and
  permission/identity caches are per process. CloudFront's origin read timeout is 60 s for that reason.
- **Both environments set `llm_provider = "bedrock"`** (the module default stays `none`). Bedrock keeps prompts inside
  the AWS account under the BAA. Requests fail until model access is granted in the Bedrock console. Verify the task
  role's `bedrock:*` actions against the endpoint the SDK client uses before relying on it.
- **`sso_require_mfa_claim` defaults to `true` and fails closed:** the API refuses ID tokens whose `amr` claim names
  no second factor. Cognito passes `amr` through only for some federation setups; see step 2.5 below.
- **The hospital IdP's OIDC client secret** (`sso_oidc_client_secret`, OIDC federation only) is stored in Terraform
  state, which is why the state bucket must be encrypted and access-controlled. SAML federation needs no secret.
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

## Go-live on AWS under a BAA (ADR-002 items 2 and 5)

Everything that can be written is in this repository. These steps need the AWS account, legal sign-off and the
hospital's identity team, in this order. Do not put real patient data anywhere before step 1.2.

**1. Agreements and access (people, not code)**
1. Create the AWS account (an organization member account for production is best) and turn on CloudTrail in all
   regions.
2. Sign the AWS Business Associate Addendum: AWS Artifact → Agreements → "AWS Business Associate Addendum" → accept
   for the account (or the organization). Keep the PDF with the hospital's compliance records.
3. Confirm every service used is HIPAA-eligible on AWS's current list: ECS Fargate, RDS for PostgreSQL, KMS, Secrets
   Manager, Cognito, Bedrock, CloudFront, WAF, S3, CloudWatch, X-Ray, SNS.
4. Request Bedrock model access (Bedrock console → Model access) in the deployment region, for the Claude models
   named by `LLM_SUMMARY_MODEL` and `LLM_ASSISTANT_MODEL`.
5. Agree with the hospital's identity team: SAML (metadata URL) or OIDC (issuer, client id and secret) federation,
   the directory groups for each role and ward, and who owns the SCIM connector.

**2. Staging**
1. Do "Before the first apply" above. In `envs/staging/terraform.tfvars` also set `sso_saml_metadata_url` (empty is
   fine at first) and `sso_provider_name`.
2. Apply staging (see "Apply"), set the secrets, confirm the alarm emails, set the GitHub environment variables.
3. Give the hospital's identity team `terraform output sso`: `saml_acs_url` and `saml_audience` for SAML. They add
   the enterprise application and send group claims.
4. Deploy (push to `main` with `AWS_DEPLOY_ENABLED=true`). `/api/health` must answer healthy.
5. Sign in at `app.staging.<domain>` through the hospital button with a test account.
   - "multi-factor authentication required" means the token has no `amr` claim. If the hospital confirms its IdP
     enforces MFA for this application, set `sso_require_mfa_claim = false`, apply and redeploy. Otherwise fix the
     IdP first.
   - On success, as an admin, map directory groups to roles and units (`POST /api/identity-providers/env/mappings`,
     `docs/api.md`), sign in again, and check the role and units.
6. SCIM: create an integration client on the admin screen, give its key to the identity team as the SCIM bearer
   token with base URL `https://api.staging.<domain>/api/scim/v2`, provision a test user, deactivate them, and check
   they cannot sign in and that an open session ends (401 within 30 seconds).
7. KMS: create a test patient and confirm in CloudTrail that `Encrypt` and `Decrypt` calls on the PHI key appear.
8. Bedrock: generate a discharge summary for a synthetic patient and check its provenance names the Bedrock model.
9. Restore drill: follow `docs/runbooks/restore-database.md` to restore staging to a point in time (PITR is 7 days
   in staging, 35 in prod) and check that patients decrypt in the restored copy.

**3. Production**
1. Apply `envs/prod` the same way (PITR 35 days, multi-AZ, deletion protection). Set `jwt_secret`, but do **not**
   generate a new `phi_kek`: step 3 copies it.
2. Freeze writes on Vercel: an admin starts read-only mode (`POST /api/downtime/start`, or `READ_ONLY_MODE=true`).
3. Copy `PHI_KEK` **exactly** from the Vercel project into `dischargeflow-prod/phi_kek` with
   `aws secretsmanager put-secret-value` (never paste it into chat or tickets). Without it every patient identifier
   is unreadable.
4. `pg_dump --format=custom` from Neon, `pg_restore` into RDS from a one-off ECS task, then run the migrate task
   (`python -m app.cli migrate`).
5. Move the data keys to KMS: run `python -m app.cli rewrap-deks` as a one-off task. It wraps every existing data key
   with the KMS key; `PHI_KEK` stays for the MRN blind index. Check that a few patients and summaries decrypt.
6. Register any further hospitals with `python -m app.cli sso-provider add --tenant <id> ...` and their Cognito app
   client (or a pool per hospital).
7. Point users at `app.<domain>` and end read-only mode on the new stack once the checks pass. Sessions do not carry
   over, so everyone signs in through the hospital IdP; the Emergent login is off (`EMERGENT_LOGIN=false`).
8. Keep the Vercel project read-only for a rollback window, then delete the Neon database and its branches.
9. Tick ADR-002 items 2 and 5 only once steps 2.5 to 2.9 and 3.1 to 3.7 are done and recorded.
