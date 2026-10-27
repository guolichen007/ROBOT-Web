#!/usr/bin/env bash
# U-Dao 公网激活 preflight（只检查，绝不修改腾讯云安全组/DNS/证书）。
# 语义：代码与配置就绪 → PUBLIC_PROFILE_CODE_READY=YES；域名/TLS 仍为占位 → PUBLIC_ACTIVATION_READY=NO。
# 用法：
#   PUBLIC_DOMAIN=firebot.example.com \
#   PUBLIC_BASE_URL=https://firebot.example.com \
#   MQTT_PUBLIC_HOSTNAME=firebot.example.com \
#   bash deploy/u-dao/scripts/50-public-preflight.sh
set -euo pipefail

REPO_ROOT="${1:-$(pwd)}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
PUBLIC_COMPOSE="$REPO_ROOT/deploy/u-dao/compose.u-dao-public.yml"
ENV_FILE="${SERVER_ENV_FILE:-$RUNTIME_ROOT/env/.env.server}"

errors=0
fail() { echo "ERROR $1" >&2; errors=$((errors + 1)); }
ok() { echo "OK $1"; }

# ---- 0) 公网 profile 存在且未默认加载（必须显式 -f 才允许公网绑定）----
[ -f "$PUBLIC_COMPOSE" ] || fail "缺少 $PUBLIC_COMPOSE"
ok "public profile 存在（默认不加载，需显式 -f）"

# ---- 1) 域名/TLS 配置检查（未就绪不判失败，但 PUBLIC_ACTIVATION_READY=NO）----
activation_ready=1
PUBLIC_DOMAIN="${PUBLIC_DOMAIN:-}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
MQTT_PUBLIC_HOSTNAME="${MQTT_PUBLIC_HOSTNAME:-}"
TLS_CERT="${PUBLIC_TLS_CERT:-$RUNTIME_ROOT/certs/nginx/public/server.crt}"
TLS_KEY="${PUBLIC_TLS_KEY:-$RUNTIME_ROOT/certs/nginx/public/server.key}"

for var in PUBLIC_DOMAIN PUBLIC_BASE_URL MQTT_PUBLIC_HOSTNAME; do
  value="${!var}"
  if [ -z "$value" ] || echo "$value" | grep -Eq 'REPLACE_|CHANGE_ME|TODO|example\.invalid|firebot\.local'; then
    fail "$var 为占位/未配置（$value）"
    activation_ready=0
  else
    ok "$var=$value"
  fi
done

if [ -s "$TLS_CERT" ] && [ -s "$TLS_KEY" ]; then
  ok "正式 TLS 证书存在（$TLS_CERT）"
  if openssl x509 -in "$TLS_CERT" -noout -ext subjectAltName 2>/dev/null | grep -qF "DNS:$PUBLIC_DOMAIN"; then
    ok "TLS 证书 SAN 覆盖 $PUBLIC_DOMAIN"
  else
    fail "TLS 证书 SAN 不含 $PUBLIC_DOMAIN"
    activation_ready=0
  fi
else
  fail "正式 TLS 证书未就位（$TLS_CERT / $TLS_KEY）"
  activation_ready=0
fi

# MQTT 公网证书（不复用 bootstrap cert）
MQTT_CERT="$RUNTIME_ROOT/secrets/mosquitto/certs/server.crt"
if [ -s "$MQTT_CERT" ] && openssl x509 -in "$MQTT_CERT" -noout -ext subjectAltName 2>/dev/null | grep -qF "DNS:$MQTT_PUBLIC_HOSTNAME"; then
  ok "MQTT 证书 SAN 覆盖 $MQTT_PUBLIC_HOSTNAME"
elif [ -n "$MQTT_PUBLIC_HOSTNAME" ]; then
  fail "MQTT 证书 SAN 不含 $MQTT_PUBLIC_HOSTNAME（bootstrap 证书不可用于公网）"
  activation_ready=0
fi

# ---- 2) MediaMTX WebRTC additionalHosts 含公网名----
if [ -f "$ENV_FILE" ] && grep -q "MEDIAMTX_WEBRTC_ADDITIONAL_HOSTS" "$ENV_FILE"; then
  hosts="$(grep -E '^MEDIAMTX_WEBRTC_ADDITIONAL_HOSTS=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
  if [ -n "$PUBLIC_DOMAIN" ] && echo "$hosts" | grep -qF "$PUBLIC_DOMAIN"; then
    ok "additionalHosts 含 $PUBLIC_DOMAIN"
  else
    fail "additionalHosts 不含公网域名（$hosts）"
    activation_ready=0
  fi
else
  fail "env 文件缺少 MEDIAMTX_WEBRTC_ADDITIONAL_HOSTS"
fi

# ---- 3) 公网 compose render + 端口断言（需 docker；无 docker 时跳过）----
if command -v docker >/dev/null 2>&1 && (docker info >/dev/null 2>&1 || sudo -n docker info >/dev/null 2>&1); then
  D=(docker)
  docker info >/dev/null 2>&1 || D=(sudo -n docker)
  cfg="$("${D[@]}" compose --env-file "$ENV_FILE" \
    -f "$REPO_ROOT/docker-compose.server.yml" \
    -f "$REPO_ROOT/deploy/u-dao/compose.u-dao.yml" \
    -f "$PUBLIC_COMPOSE" config --format json 2>/dev/null || true)"
  if [ -n "$cfg" ]; then
    ok "public compose render 成功"
    echo "$cfg" | python3 -I -c "
import json, sys
cfg = json.load(sys.stdin)
host_ports = {}
for svc, spec in cfg['services'].items():
    for p in spec.get('ports', []) or []:
        host_ports.setdefault(p.get('published'), []).append((svc, p.get('host_ip', '0.0.0.0'), p.get('protocol')))
public = {k: v for k, v in host_ports.items() if any(ip == '0.0.0.0' for _, ip, _ in v)}
forbidden = {'1883', '5432', '6379', '8554', '8889', '9997', '9998', '18080'}
assert not (set(public) & forbidden), f'forbidden public ports: {set(public) & forbidden}'
for port in ('8883', '8890', '8189'):
    assert port in public, f'missing public port {port}'
dupes = {k: v for k, v in public.items() if len(v) > 1}
assert not dupes, f'duplicate public mappings: {dupes}'
print('OK 公网端口断言：8883/8890/8189 各一处，无 1883/5432/6379/8554/8889/9997/9998/18080')
" || fail "公网端口断言失败"
  else
    fail "public compose render 失败"
  fi
else
  echo "SKIP public compose render（docker 不可用）"
fi

# ---- 3.5) shared ingress 生产模板与端口（模板存在 + 端口 80/443 仅由平台层定义）----
INGRESS_TEMPLATE="$REPO_ROOT/deploy/u-dao/ingress/conf.d/firebot.public.conf.template"
[ -f "$INGRESS_TEMPLATE" ] || fail "缺少 ingress 生产模板 $INGRESS_TEMPLATE"
ok "ingress 生产模板存在（80 redirect + 443 ssl，需 70-prepare-public-ingress.sh 显式激活）"
PUBLIC_COMPOSE_PORTS="$REPO_ROOT/deploy/u-dao/ingress/compose.public.yml.template"
[ -f "$PUBLIC_COMPOSE_PORTS" ] && ok "ingress 公网端口模板存在（80/443）" || fail "缺少 ingress 公网端口模板"

# ---- 4) DOWNLINK 恒 false + allowlist 空 ----
if [ -f "$ENV_FILE" ] && grep -qE '^VEHICLE_AGENT_V1_DOWNLINK_ENABLED=true' "$ENV_FILE"; then
  fail "VEHICLE_AGENT_V1_DOWNLINK_ENABLED 不得为 true（公网激活前）"
else
  ok "DOWNLINK=false"
fi
if [ -f "$ENV_FILE" ] && grep -qE '^VEHICLE_AGENT_V1_DOWNLINK_ALLOWLIST=[^[:space:]]+' "$ENV_FILE"; then
  fail "VEHICLE_AGENT_V1_DOWNLINK_ALLOWLIST 必须为空（公网激活前）"
else
  ok "DOWNLINK_ALLOWLIST=empty"
fi

# ---- 结论 ----
echo "PUBLIC_PROFILE_CODE_READY=YES"
if [ "$activation_ready" = 1 ] && [ "$errors" -eq 0 ]; then
  echo "PUBLIC_ACTIVATION_READY=YES（代码/配置就绪；激活仍需 OWNER 完成安全组/DNS/证书部署）"
else
  echo "PUBLIC_ACTIVATION_READY=NO（域名/TLS 未就绪，属预期；不判开发失败）"
fi
[ "$errors" -eq 0 ] || { echo "PUBLIC_PREFLIGHT=FAIL count=$errors"; exit 1; }
echo "PUBLIC_PREFLIGHT=PASS"
