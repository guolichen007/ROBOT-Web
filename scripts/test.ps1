$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

docker compose -f compose.test.yml --profile full down --volumes --remove-orphans
if ($LASTEXITCODE -ne 0) { throw 'Pre-test cleanup failed.' }
Write-Host '[1/5] Building the isolated Python test image...'
docker compose -f compose.test.yml build tests
Write-Host '[2/5] Starting isolated PostgreSQL, Redis and Mosquitto...'
docker compose -f compose.test.yml up -d postgres redis mosquitto
try {
    Write-Host '[3/5] Running migrations, seed, Ruff, mypy and Pytest...'
    docker compose -f compose.test.yml run --rm tests sh -c 'cd /workspace && python scripts/test_migration_matrix.py && cd /workspace/apps/api && alembic upgrade head && python -m app.db.seed && cd /workspace && ruff check apps/api services scripts && ruff format --check apps/api services scripts && mypy apps/api/app services/protocol.py && for file in services/*/main.py; do mypy --follow-imports=skip "$file"; done && pytest apps/api/tests services/protocol-tester/tests'
    if ($LASTEXITCODE -ne 0) { throw 'Backend/test profile failed.' }
    Write-Host '[4/5] Running frontend install, typecheck, lint, unit tests and build (npm ci can take 1-2 minutes on first run)...'
    docker volume create firebot-test-web-node-modules | Out-Null
    docker run --rm -v "${repo}\apps\web:/app" -v firebot-test-web-node-modules:/app/node_modules -w /app node:24.19.0-alpine3.24 sh -c 'npm ci --ignore-scripts --no-audit --no-fund && npm run typecheck && npm run lint && npm run format:check && npm run test && npm run build'
    if ($LASTEXITCODE -ne 0) { throw 'Frontend checks failed.' }
    Write-Host '[5/5] Checking generated protocol model drift...'
    docker run --rm -v "${repo}:/workspace" -w /workspace firebot-dev-api python scripts/check_protocol_drift.py
    if ($LASTEXITCODE -ne 0) { throw 'Protocol drift check failed.' }
} finally {
    Write-Host 'Cleaning the isolated TEST profile...'
    docker compose -f compose.test.yml --profile full down --volumes --remove-orphans
}
