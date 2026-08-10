# Backup and Restore

Run `scripts/backup.ps1`. It writes an ignored timestamped folder containing a PostgreSQL custom-format dump, asset copy, non-secret service configuration, manifest and SHA-256 file. Secret material must be backed up separately by the deployment owner using encrypted storage and access controls.

Restore only to the intended Firebot environment:

```powershell
.\scripts\restore.ps1 -BackupDir C:\path\to\backup -ConfirmRestore
```

The restore script resolves the exact backup path, verifies SHA-256, waits for PostgreSQL readiness, checks every native command exit code, restores PostgreSQL with `--clean --if-exists`, copies assets, reapplies migrations and reports counts for users, maps, telemetry, fire events, tasks, audit logs and commands. A smoke test must then verify login, map, R001 history, alarms, tasks and audit through the API/browser.

RPO/RTO = To Be Confirmed by deployment owner. This repository supplies configurable, testable capability and does not invent a commercial SLA.
