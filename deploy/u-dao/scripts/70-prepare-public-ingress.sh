#!/usr/bin/env bash
# U-Dao shared ingress 生产公网激活（platform 资源；仅在 OWNER 提供正式域名/TLS 后执行）。
# 流程：生成 firebot-public.conf → nginx -t → reload；错误证书/SAN/域名 → fail-closed 不激活。
# 不改变 bootstrap 18000/18443；Firebot 部署/回滚仍禁止操作 u-dao-ingress project。
# 用法：
#   PUBLIC_DOMAIN=firebot.example.com \
#   PUBLIC_TLS_CERT=/srv/u-dao/platform/ingress/tls/public/server.crt \
#   PUBLIC_TLS_KEY=/srv/u-dao/platform/ingress/tls/public/server.key \
#   bash deploy/u-dao/scripts/70-prepare-public-ingress.sh
set -euo pipefail

REPO_ROOT="${1:-$(pwd)}"
INGRESS_ROOT="${U_DAO_INGRESS_ROOT:-/srv/u-dao/platform/ingress}"
TEMPLATE="$REPO_ROOT/deploy/u-dao/ingress/conf.d/firebot.public.conf.template"

fail() { echo "PUBLIC_INGRESS=FAIL: $1" >&2; exit 1; }

PUBLIC_DOMAIN="${PUBLIC_DOMAIN:-}"
PUBLIC_TLS_CERT="${PUBLIC_TLS_CERT:-}"
PUBLIC_TLS_KEY="${PUBLIC_TLS_KEY:-}"

[ -n "$PUBLIC_DOMAIN" ] || fail "PUBLIC_DOMAIN 未提供"
echo "$PUBLIC_DOMAIN" | grep -Eq 'REPLACE_|CHANGE_ME|TODO|example\.invalid|firebot\.local' \
  && fail "PUBLIC_DOMAIN 为占位值（$PUBLIC_DOMAIN）"
[ -s "$PUBLIC_TLS_CERT" ] || fail "证书不存在或为空（$PUBLIC_TLS_CERT）"
[ -s "$PUBLIC_TLS_KEY" ] || fail "私钥不存在或为空（$PUBLIC_TLS_KEY）"

# SAN 必须覆盖正式域名（证书私钥不回显）
openssl x509 -in "$PUBLIC_TLS_CERT" -noout -ext subjectAltName 2>/dev/null | grep -qF "DNS:$PUBLIC_DOMAIN" \
  || fail "证书 SAN 不含 DNS:$PUBLIC_DOMAIN"

# 生成 firebot-public.conf（模板替换）
TARGET_CONF="$INGRESS_ROOT/conf.d/firebot-public.conf"
sed -e "s|__PUBLIC_DOMAIN__|$PUBLIC_DOMAIN|g" \
    -e "s|__PUBLIC_TLS_CERT__|$PUBLIC_TLS_CERT|g" \
    -e "s|__PUBLIC_TLS_KEY__|$PUBLIC_TLS_KEY|g" \
    "$TEMPLATE" > "$TARGET_CONF"
chmod 644 "$TARGET_CONF"
echo "已生成 $TARGET_CONF"

# nginx -t 通过后才 reload（fail-closed）
if command -v docker >/dev/null 2>&1 && (docker info >/dev/null 2>&1 || sudo -n docker info >/dev/null 2>&1); then
  D=(docker)
  docker info >/dev/null 2>&1 || D=(sudo -n docker)
  CID="$("${D[@]}" compose -p u-dao-ingress ps -q nginx 2>/dev/null | head -1 || true)"
  if [ -n "$CID" ]; then
    "${D[@]}" exec "$CID" nginx -t || { rm -f "$TARGET_CONF"; fail "nginx -t 失败，已回滚生成文件"; }
    "${D[@]}" exec "$CID" nginx -s reload || fail "reload 失败"
    echo "PUBLIC_INGRESS=ACTIVE domain=$PUBLIC_DOMAIN（80/443 需 platform compose 端口开放后生效）"
  else
    echo "ingress 容器未运行：配置已生成但未 reload（PUBLIC_INGRESS=PENDING）"
  fi
else
  echo "docker 不可用：配置已生成但未 reload（PUBLIC_INGRESS=PENDING）"
fi
