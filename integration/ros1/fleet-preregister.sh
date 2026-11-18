#!/usr/bin/env bash
# fleet-preregister.sh — 服务器侧 Fleet 预登记（firebotctl fleet preregister 的受控实现）。
#
# 语义（REAL_VEHICLE_CONNECTIVITY_FIELD_GATE / P0-A）：
#   - 建立 Fleet stable identity（Robot row，enabled=false：允许 MQTT 上行/遥测，控制默认关闭）
#     + 最小只读 assignment 基线（supported_commands=[]、location_enabled=false）。
#   - --site-code 必须显式传入；不存在 → SITE_NOT_FOUND，inactive → SITE_INACTIVE（禁止猜 Site）。
#   - 幂等：完全相同 → EXISTS；site/name 不一致 → CONFLICT（绝不自动更新）。
#   - 绝对不生成 MQTT password / 修改 Mosquitto / 签发 token / 生成 SRT credential /
#     打开 downlink / 修改 allowlist / 启动车辆 / 修改任何已存在 Robot。
#   - 业务逻辑唯一实现在 apps/api/app/modules/fleet/ops.py（SQLAlchemy model）。
#
# 用法（服务器上，ROBOT-Web 仓库根）：
#   firebotctl fleet preregister <DEVICE_ID> --name <DISPLAY_NAME> --site-code <SITE_CODE>
set -euo pipefail

DEVICE_ID="${1:-}"
if ! echo "$DEVICE_ID" | grep -qE '^[A-Za-z0-9_-]{1,64}$'; then
  echo "ERROR: DEVICE_ID 非法（只允许 [A-Za-z0-9_-]{1,64}）" >&2
  exit 1
fi
shift

NAME=""
SITE_CODE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="${2:-}"; shift 2 ;;
    --site-code) SITE_CODE="${2:-}"; shift 2 ;;
    *) echo "ERROR: 未知参数 $1（用法: --name <N> --site-code <S>）" >&2; exit 1 ;;
  esac
done
[ -n "$NAME" ] || { echo "ERROR: 必须显式提供 --name（显示名称，不得猜测）" >&2; exit 1; }
[ -n "$SITE_CODE" ] || { echo "ERROR: 必须显式提供 --site-code（禁止随机选 Site/默认复制他车 Site）" >&2; exit 1; }
echo "$NAME" | grep -qE '^[^[:space:]'\''"]{1,128}$' || { echo "ERROR: --name 非法" >&2; exit 1; }
echo "$SITE_CODE" | grep -qE '^[A-Za-z0-9_-]{1,64}$' || { echo "ERROR: --site-code 非法" >&2; exit 1; }

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${U_DAO_REPO_ROOT:-$(cd "$SELF_DIR/../.." && pwd)}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
ENV_FILE="${SERVER_ENV_FILE:-$RUNTIME_ROOT/env/.env.server}"

# shellcheck source=scripts/docker_cmd.sh
source "$REPO_ROOT/scripts/docker_cmd.sh"
docker_cmd_detect || { echo "FLEET_PREREGISTER=FAIL（docker 不可用）" >&2; exit 1; }

COMPOSE=("${DOCKER[@]}" compose --env-file "$ENV_FILE")
for f in "$REPO_ROOT/docker-compose.server.yml" "$REPO_ROOT/deploy/u-dao/compose.u-dao.yml"; do
  COMPOSE+=(-f "$f")
done

"${COMPOSE[@]}" exec -T api python -c "
import sys
from app.modules.fleet.ops import run_cli
sys.exit(run_cli(['preregister', '$DEVICE_ID', '--name', '''$NAME''', '--site-code', '''$SITE_CODE''']))
"
