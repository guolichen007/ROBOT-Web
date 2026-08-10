# Acceptance Tests

Automated gates: Ruff, Ruff format, mypy per deployable module, Pytest, Alembic empty/upgrade migration, schema validation/drift, ESLint, Prettier, Vue typecheck, Vitest, Vite build, Docker health smoke and Playwright Chromium.

Browser acceptance records must cover login, R001 online movement, map heading, two-client lease conflict, manual hold/release, hidden/blur stop, ACK state, alarm linkage, A-12 manual alarm, extinguish lifecycle, map mismatch, history/audit/settings and video OFFLINE state.

Core fault tests cover duplicate/out-of-order/invalid MQTT, rebooted `boot_id`, timestamp skew, late/duplicate/wrong ACK, broker/Redis/API/dispatcher restart, snapshot gap resync, expired-command non-replay and worker idempotency.

Never convert a missing ACK or offline publish attempt to success. Failed tests are fixed or recorded as a real blocker; they are not deleted.
