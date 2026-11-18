#!/usr/bin/env bash
# fleet-sites.sh — 服务器侧只读列出 Fleet Site（firebotctl fleet sites 的受控实现）。
#
# 只读输出 SITE_CODE / SITE_NAME / ACTIVE / ROBOT_COUNT，不含任何数据库敏感信息。
# 业务逻辑唯一实现在 apps/api/app/modules/fleet/ops.py（SQLAlchemy model，禁止现场手写 SQL）。
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${U_DAO_REPO_ROOT:-$(cd "$SELF_DIR/../.." && pwd)}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
ENV_FILE="${SERVER_ENV_FILE:-$RUNTIME_ROOT/env/.env.server}"

# shellcheck source=scripts/docker_cmd.sh
source "$REPO_ROOT/scripts/docker_cmd.sh"
docker_cmd_detect || { echo "FLEET_SITES=FAIL（docker 不可用）" >&2; exit 1; }

COMPOSE=("${DOCKER[@]}" compose --env-file "$ENV_FILE")
for f in "$REPO_ROOT/docker-compose.server.yml" "$REPO_ROOT/deploy/u-dao/compose.u-dao.yml"; do
  COMPOSE+=(-f "$f")
done

"${COMPOSE[@]}" exec -T api python -c "
import sys
from app.modules.fleet.ops import run_cli
sys.exit(run_cli(['sites']))
"
