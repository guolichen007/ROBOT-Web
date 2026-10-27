#!/usr/bin/env bash
# U-Dao shared ingress 生产公网：prepare + validate + activate（platform 资源）。
# 默认模式只准备与校验（不改变公网监听）；只有显式 --activate 才允许改变 80/443。
# Firebot 项目不拥有也不得 down u-dao-ingress project。
# 用法：
#   PUBLIC_DOMAIN=firebot.example.com \
#   PUBLIC_TLS_CERT=/srv/u-dao/platform/ingress/tls/public/server.crt \
#   PUBLIC_TLS_KEY=/srv/u-dao/platform/ingress/tls/public/server.key \
#   bash deploy/u-dao/scripts/70-prepare-public-ingress.sh              # prepare+validate
#   ...同上 --activate                                                   # 激活（需全部 gate PASS）
set -euo pipefail

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
INGRESS_ROOT="${U_DAO_INGRESS_ROOT:-/srv/u-dao/platform/ingress}"
TEMPLATE="$REPO_ROOT/deploy/u-dao/ingress/conf.d/firebot.public.conf.template"
PUBLIC_OVERRIDE="$REPO_ROOT/deploy/u-dao/ingress/compose.public.yml"
MODE="${2:-prepare}"

fail() { echo "PUBLIC_INGRESS=FAIL: $1" >&2; exit 1; }

PUBLIC_DOMAIN="${PUBLIC_DOMAIN:-}"
PUBLIC_TLS_CERT="${PUBLIC_TLS_CERT:-}"
PUBLIC_TLS_KEY="${PUBLIC_TLS_KEY:-}"

[ -n "$PUBLIC_DOMAIN" ] || fail "PUBLIC_DOMAIN 未提供"
echo "$PUBLIC_DOMAIN" | grep -Eq 'REPLACE_|CHANGE_ME|TODO|example\.invalid|firebot\.local' \
  && fail "PUBLIC_DOMAIN 为占位值（$PUBLIC_DOMAIN）"
[ -s "$PUBLIC_TLS_CERT" ] || fail "证书不存在或为空（$PUBLIC_TLS_CERT）"
[ -s "$PUBLIC_TLS_KEY" ] || fail "私钥不存在或为空（$PUBLIC_TLS_KEY）"
openssl x509 -in "$PUBLIC_TLS_CERT" -noout -ext subjectAltName 2>/dev/null | grep -qF "DNS:$PUBLIC_DOMAIN" \
  || fail "证书 SAN 不含 DNS:$PUBLIC_DOMAIN"
# 宿主路径 → 容器内路径映射（ingress 容器挂载 $INGRESS_ROOT/tls:/etc/nginx/tls）
case "$PUBLIC_TLS_CERT" in
  "$INGRESS_ROOT"/tls/*) CONTAINER_CERT="/etc/nginx/tls/${PUBLIC_TLS_CERT#"$INGRESS_ROOT"/tls/}" ;;
  *) fail "证书必须位于 $INGRESS_ROOT/tls/ 下（容器挂载可见性）" ;;
esac
case "$PUBLIC_TLS_KEY" in
  "$INGRESS_ROOT"/tls/*) CONTAINER_KEY="/etc/nginx/tls/${PUBLIC_TLS_KEY#"$INGRESS_ROOT"/tls/}" ;;
  *) fail "私钥必须位于 $INGRESS_ROOT/tls/ 下（容器挂载可见性）" ;;
esac

detect_docker() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif command -v docker >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    fail "docker 不可用"
  fi
}

# ---- 1) 生成 firebot-public.conf（模板替换）----
TARGET_CONF="$INGRESS_ROOT/conf.d/firebot-public.conf"
sed -e "s|__PUBLIC_DOMAIN__|$PUBLIC_DOMAIN|g" \
    -e "s|__PUBLIC_TLS_CERT__|$CONTAINER_CERT|g" \
    -e "s|__PUBLIC_TLS_KEY__|$CONTAINER_KEY|g" \
    "$TEMPLATE" > "$TARGET_CONF"
chmod 644 "$TARGET_CONF"
echo "CONF_RENDER=PASS $TARGET_CONF"

# ---- 2) platform public compose 校验（base + override 可渲染；80/443 只属于 u-dao-ingress）----
detect_docker
BASE_COMPOSE="$INGRESS_ROOT/docker-compose.yml"
[ -f "$BASE_COMPOSE" ] || fail "platform base compose 不存在（$BASE_COMPOSE）"
"${DOCKER[@]}" compose -p u-dao-ingress -f "$BASE_COMPOSE" -f "$PUBLIC_OVERRIDE" config --quiet \
  || fail "platform public compose render 失败"
echo "PUBLIC_COMPOSE_RENDER=PASS"

# ---- 3) forbidden ports 断言（5432/6379/1883/8554/9997/9998 不得公网）----
cfg="$("${DOCKER[@]}" compose -p u-dao-ingress -f "$BASE_COMPOSE" -f "$PUBLIC_OVERRIDE" config --format json 2>/dev/null || true)"
echo "$cfg" | python3 -I -c "
import json, sys
cfg = json.load(sys.stdin)
forbidden = {'5432', '6379', '1883', '8554', '9997', '9998'}
public = set()
for svc, spec in cfg['services'].items():
    for p in spec.get('ports', []) or []:
        public.add(str(p.get('published')))
bad = forbidden & public
assert not bad, f'forbidden public ports: {bad}'
print('FORBIDDEN_PORTS=PASS')
"

# ---- 4) nginx 静态校验（生成 conf 后 nginx -t；不 reload）----
CID="$("${DOCKER[@]}" compose -p u-dao-ingress ps -q nginx 2>/dev/null | head -1 || true)"
if [ -n "$CID" ]; then
  "${DOCKER[@]}" exec "$CID" nginx -t || { rm -f "$TARGET_CONF"; fail "nginx -t 失败，已回滚生成文件"; }
  echo "NGINX_TEST=PASS"
else
  echo "SKIP nginx -t（ingress 容器未运行）"
fi

echo "PUBLIC_INGRESS_PREPARED=YES"
echo "PUBLIC_INGRESS_ACTIVE=NO"

# ---- 5) 激活（仅 --activate；全部 gate PASS 才允许 up -d）----
if [ "$MODE" = "--activate" ]; then
  # DOWNLINK 必须 false
  ENV_FILE="${SERVER_ENV_FILE:-/srv/u-dao/projects/firebot/runtime/env/.env.server}"
  grep -qE '^VEHICLE_AGENT_V1_DOWNLINK_ENABLED=true' "$ENV_FILE" 2>/dev/null \
    && fail "VEHICLE_AGENT_V1_DOWNLINK_ENABLED 不得为 true（激活公网前）"
  "${DOCKER[@]}" compose -p u-dao-ingress -f "$BASE_COMPOSE" -f "$PUBLIC_OVERRIDE" up -d \
    || fail "public ingress up 失败"
  NEW_CID="$("${DOCKER[@]}" compose -p u-dao-ingress ps -q nginx 2>/dev/null | head -1 || true)"
  [ -n "$NEW_CID" ] && "${DOCKER[@]}" exec "$NEW_CID" nginx -t && "${DOCKER[@]}" exec "$NEW_CID" nginx -s reload
  sleep 2
  if sudo -n ss -lnt | awk '{print $4}' | grep -Eq '[:.]80$|[:.]443$'; then
    echo "PUBLIC_LISTEN=80/443 CONFIRMED"
  else
    fail "激活后未观察到 80/443 监听"
  fi
  echo "PUBLIC_INGRESS_ACTIVE=YES domain=$PUBLIC_DOMAIN"
fi
