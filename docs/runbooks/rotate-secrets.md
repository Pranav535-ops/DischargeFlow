# Rotate secrets

Application secrets live in Secrets Manager under `dischargeflow-<env>/` (created empty by Terraform; values are
never in Terraform state or Git). ECS reads them when a task starts, so a rotation takes effect on the next
deployment: force one with

```bash
aws ecs update-service --cluster dischargeflow-<env> --service api --force-new-deployment
```

Set values from a trusted workstation, never paste them into chat, tickets or CI logs:

```bash
aws secretsmanager put-secret-value --secret-id dischargeflow-<env>/<name> --secret-string "$(<generator>)"
```

## JWT_SECRET (signs 15-minute access tokens)

Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

Rotating invalidates every access token immediately. Users don't sign in again: the SPA retries a 401 once after
`/api/auth/refresh`, and refresh tokens are random values checked against the database, not JWTs. Rotate at any
time; rotate **immediately** if the secret may have leaked.

## PHI_KEK (wraps every patient's data key)

There is no recovery path for this key. Keep an offline backup of every KEK that has ever wrapped data until no
backup within the retention window depends on it ([restore-database.md](restore-database.md)).

1. Generate the new key: `python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"` and
   back it up offline.
2. Create `dischargeflow-<env>/phi_kek_previous` holding the **current** key, and add it to the API task definition
   as the `PHI_KEK_PREVIOUS` secret (comma-separated if several are in flight).
3. Put the **new** key into `dischargeflow-<env>/phi_kek` and force a new deployment. New patients are now wrapped
   with the new key; existing ones still unwrap with the previous key.
4. Re-wrap every existing data key with the new KEK, as a one-off task with the same task definition:

   ```bash
   aws ecs run-task ... --overrides '{"containerOverrides":[{"name":"api","command":["python","-m","app.cli","rewrap-deks"]}]}'
   ```

   It prints `{"rewrapped": N, "already_current": M}` and is safe to re-run. Field ciphertext is untouched; only each
   row's small wrapped key changes.
5. Run it again and confirm `"rewrapped": 0`. Then remove `PHI_KEK_PREVIOUS` from the task definition and deploy.
6. Keep the old key offline for as long as backups from before step 4 are retained.

If `rewrap-deks` fails with "DEK is not wrapped by PHI_KEK or any key in PHI_KEK_PREVIOUS", a row was wrapped by a
key that isn't configured. Stop and find that key before going further; do not remove any key.

Moving the KEK into AWS KMS (`KeyProvider` in `app/core/crypto.py`) is the planned next step: KMS key deletion makes
crypto-shredding effective in old backups too.

## Database password

RDS manages it in its own Secrets Manager secret (`manage_master_user_password`) and can rotate it on a schedule.
ECS injects it as `DB_PASSWORD` at task start, so after a rotation, force a new deployment. Connections that are
already open keep working until they close.

## ANTHROPIC_API_KEY

Only used when `llm_provider = "anthropic"`. Create a replacement key in the Anthropic Console, put it in
`dischargeflow-<env>/anthropic_api_key`, force a new deployment, then revoke the old key in the Console.

## Suspected leak

1. Rotate the secret immediately (sections above).
2. For `JWT_SECRET`, also revoke all sessions:
   `UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'secret_rotation' WHERE revoked_at IS NULL;`
   (run as the database owner; users sign in again).
3. For `PHI_KEK`, treat it as a possible PHI breach: rotate (above), then follow the organisation's HIPAA breach
   assessment process. Check the audit log for unusual reads.
4. Record what happened, when, and what was rotated.
