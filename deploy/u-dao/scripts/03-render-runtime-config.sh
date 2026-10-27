#!/usr/bin/env bash
# 渲染 U-Dao runtime 配置（真值不入 Git）：
#   1) runtime/env/mediamtx.server.u-dao.yml：以仓库 mediamtx.server.yml 为模板，
#      注入 runtime/secrets/app/srt_publish_passphrase（per-path srtPublishPassphrase）。
#   2) shared ingress 片段实例化：deploy/u-dao/ingress/conf.d/firebot.conf.template
#      → /srv/u-dao/platform/ingress/conf.d/firebot.conf（platform 目录，Firebot 只贡献模板）。
# 用法（仓库/release 根执行）：bash deploy/u-dao/scripts/03-render-runtime-config.sh
set -euo pipefail

REPO_ROOT="${1:-$(pwd)}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
INGRESS_CONF_D="${U_DAO_INGRESS_CONF_D:-/srv/u-dao/platform/ingress/conf.d}"

fail() { echo "RENDER_RUNTIME_CONFIG=FAIL: $1" >&2; exit 1; }

pass="$RUNTIME_ROOT/secrets/app/srt_publish_passphrase"
[ -s "$pass" ] || fail "缺少 $pass（先执行 02-gen-bootstrap-secrets.sh）"
[ -f "$REPO_ROOT/infra/mediamtx/mediamtx.server.yml" ] || fail "请在 ROBOT-Web 仓库/release 根执行"
[ -f "$REPO_ROOT/deploy/u-dao/ingress/conf.d/firebot.conf.template" ] || fail "缺少 ingress 片段模板"

umask 077
mkdir -p "$RUNTIME_ROOT/env" "$INGRESS_CONF_D"

# 注入 SRT 发布加密 passphrase（与发布鉴权 token 独立，禁止复用；真值仅存在于 runtime 600 文件）
awk -v pw="$(cat "$pass")" '
  /^[[:space:]]*srtPublishPassphrase:[[:space:]]*$/ {
    print "    srtPublishPassphrase: \"" pw "\""
    next
  }
  { print }
' "$REPO_ROOT/infra/mediamtx/mediamtx.server.yml" > "$RUNTIME_ROOT/env/mediamtx.server.u-dao.yml"
chmod 600 "$RUNTIME_ROOT/env/mediamtx.server.u-dao.yml"
grep -q 'srtPublishPassphrase: "' "$RUNTIME_ROOT/env/mediamtx.server.u-dao.yml" \
  || fail "渲染结果缺少 srtPublishPassphrase"
# 渲染产物不得与模板一致（必须有真值）
! cmp -s "$REPO_ROOT/infra/mediamtx/mediamtx.server.yml" "$RUNTIME_ROOT/env/mediamtx.server.u-dao.yml" \
  || fail "渲染结果与模板一致（passphrase 未注入）"

cp "$REPO_ROOT/deploy/u-dao/ingress/conf.d/firebot.conf.template" "$INGRESS_CONF_D/firebot.conf"
echo "RENDER_RUNTIME_CONFIG=PASS mediamtx=$RUNTIME_ROOT/env/mediamtx.server.u-dao.yml ingress=$INGRESS_CONF_D/firebot.conf"
