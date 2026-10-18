# Restore the database

Targets (ADR-001): RPO 5 minutes, RTO 1 hour. RDS keeps automated backups with point-in-time recovery
(35 days in prod, 7 in staging), encrypted with the stack's KMS key.

## Point-in-time restore

RDS restores into a **new** instance; the original is left untouched until you switch over.

1. Pick the restore time: just before the incident. Audit log timestamps (`/api/audit/logs`) help bracket bad
   writes. Note that the time is in UTC.
2. Restore:

   ```bash
   aws rds restore-db-instance-to-point-in-time \
     --source-db-instance-identifier dischargeflow-<env> \
     --target-db-instance-identifier dischargeflow-<env>-restore-<yyyymmddhhmm> \
     --restore-time <YYYY-MM-DDTHH:MM:SSZ> \
     --db-subnet-group-name dischargeflow-<env> \
     --vpc-security-group-ids <db security group id> \
     --db-parameter-group-name dischargeflow-<env>-pg16 \
     --no-publicly-accessible
   aws rds wait db-instance-available --db-instance-identifier dischargeflow-<env>-restore-<yyyymmddhhmm>
   ```

3. **Verify before switching.** Run a one-off ECS task from the current API task definition with `DB_HOST`
   overridden to the restored endpoint and the command `python -m app.cli migrate` (a no-op if the schema is current),
   then check row counts and the audit chain (`GET /api/audit/verify` as an admin against a staging copy).
4. **Switch over** (pick one):
   - Rename: rename the broken instance to `...-broken`, then the restored one to `dischargeflow-<env>`. The endpoint
     hostname follows the identifier, so the app reconnects without a config change. Then run
     `terraform plan` and confirm it shows no replacement of `aws_db_instance.this`.
   - Or point the service at the new endpoint by changing `DB_HOST` (task definition) and redeploy.
5. Keep the broken instance (deletion protection on) until the incident review is done; it is evidence.

## Things that are not in the backup

- **PHI keys.** Patient identifiers decrypt only with the `PHI_KEK` that wrapped their data keys. A restore from
  before a KEK rotation needs the old KEK in `PHI_KEK_PREVIOUS` until `python -m app.cli rewrap-deks` has run
  (see [rotate-secrets.md](rotate-secrets.md)). Losing every copy of a KEK makes those patients' identifiers
  permanently unreadable.
- **Erasures.** A restore from before a patient's erasure brings their data back, because crypto-shredding destroyed
  only the live row's key. Re-apply erasures recorded in the audit log (action `GDPR_ERASURE`) after any restore.
- The audit chain is restored with the data. Entries written after the restore time are lost with it; export them
  from the broken instance first if they matter.

## Practice

Restore staging to a point in time once a quarter and time it against the 1-hour RTO.
