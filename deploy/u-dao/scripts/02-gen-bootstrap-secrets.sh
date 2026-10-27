#!/usr/bin/env bash
# U-Dao bootstrap runtime 生成：secrets（600/700，随机不回显）+ 本地 CA + MQTT/nginx bootstrap 证书
# + .env.server（来自 deploy/u-dao/.env.u-dao.example）+ mosquitto passwords/acl + /etc/hosts 本地条目。
# BOOTSTRAP_LOCAL_ONLY：非公网证书，真实车辆接入前必须替换（标记文件 + 验收报告说明）。
# 用法（在 ROBOT-Web 仓库根执行）：bash deploy/u-dao/scripts/02-gen-bootstrap-secrets.sh
# 幂等保护：runtime/env/.env.server 已存在则拒绝重跑（防覆盖真实配置）。
set -euo pipefail

REPO_ROOT="${1:-$(pwd)}"
RUNTIME_ROOT="${U_DAO_RUNTIME_ROOT:-/srv/u-dao/projects/firebot/runtime}"
INGRESS_TLS="${U_DAO_INGRESS_TLS:-/srv/u-dao/platform/ingress/tls}"
HOST_ALIASES="${U_DAO_HOST_ALIASES:-firebot.local}"
ADD_HOSTS="${ADD_HOSTS:-1}"
MOSQ_IMAGE="eclipse-mosquitto:2.1.2-alpine"

fail() { echo "BOOTSTRAP_SECRETS=FAIL: $1" >&2; exit 1; }

[ -f "$REPO_ROOT/infra/mosquitto/acl.example" ] || fail "请在 ROBOT-Web 仓库根执行或传参仓库路径（当前 $REPO_ROOT）"
[ -f "$REPO_ROOT/deploy/u-dao/.env.u-dao.example" ] || fail "缺少 deploy/u-dao/.env.u-dao.example"
command -v openssl >/dev/null 2>&1 || fail "openssl 未安装"
[ -f "$RUNTIME_ROOT/env/.env.server" ] && fail ".env.server 已存在（$RUNTIME_ROOT/env/.env.server）；如需重生成先人工备份并移除（幂等保护）"

# ---- Docker 命令检测（docker → sudo -n docker → fail；与 scripts/docker_cmd.sh 同语义）----
detect_docker() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif command -v docker >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo -n docker)
  else
    fail "docker 不可用（docker 与 sudo -n docker 均失败）"
  fi
}

umask 077
mkdir -p "$RUNTIME_ROOT"/env "$RUNTIME_ROOT"/secrets/app "$RUNTIME_ROOT"/secrets/postgres \
         "$RUNTIME_ROOT"/secrets/redis "$RUNTIME_ROOT"/secrets/mosquitto/certs "$RUNTIME_ROOT"/secrets/nginx \
         "$RUNTIME_ROOT"/certs/ca "$RUNTIME_ROOT"/certs/mosquitto "$RUNTIME_ROOT"/certs/nginx
chmod 700 "$RUNTIME_ROOT"/env "$RUNTIME_ROOT"/secrets "$RUNTIME_ROOT"/certs

gen() { # $1=相对路径 $2=base64 字节数
  openssl rand -base64 "$2" > "$RUNTIME_ROOT/secrets/$1"
  chmod 600 "$RUNTIME_ROOT/secrets/$1"
  echo "  secrets/$1（$(wc -c < "$RUNTIME_ROOT/secrets/$1") 字节，值不回显）"
}

echo "== 生成 secrets =="
gen app/jwt_secret 48
gen app/refresh_secret 48
gen app/csrf_secret 48
gen app/bootstrap_admin_password 32
gen app/mqtt_platform_password 36
gen app/media_publish_token 36
gen app/srt_publish_passphrase 36 # SRT 加密 passphrase：与 media_publish_token 严格独立，禁止复用
gen postgres/password 32
gen redis/password 32
printf 'postgresql+psycopg://firebot:%s@postgres:5432/firebot' "$(cat "$RUNTIME_ROOT/secrets/postgres/password")" > "$RUNTIME_ROOT/secrets/app/database_url"
printf 'redis://:%s@redis:6379/0' "$(cat "$RUNTIME_ROOT/secrets/redis/password")" > "$RUNTIME_ROOT/secrets/app/redis_url"
chmod 600 "$RUNTIME_ROOT/secrets/app/database_url" "$RUNTIME_ROOT/secrets/app/redis_url"

# ---- bootstrap CA（本地验收专用）----
echo "== 生成本地 CA =="
openssl req -x509 -newkey rsa:3072 -sha256 -days 825 -nodes \
  -keyout "$RUNTIME_ROOT/certs/ca/ca.key" -out "$RUNTIME_ROOT/certs/ca/ca.crt" \
  -subj "/CN=U-Dao Bootstrap CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
chmod 600 "$RUNTIME_ROOT/certs/ca/ca.key"; chmod 644 "$RUNTIME_ROOT/certs/ca/ca.crt"

sign_cert() { # $1=输出目录 $2=CN $3=SAN 行
  local dir="$1" cn="$2" san="$3"
  printf 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n%s\n' "$san" > "$dir/ext.cnf"
  openssl req -newkey rsa:2048 -nodes -keyout "$dir/server.key" -out "$dir/server.csr" -subj "/CN=$cn" 2>/dev/null
  openssl x509 -req -in "$dir/server.csr" -CA "$RUNTIME_ROOT/certs/ca/ca.crt" -CAkey "$RUNTIME_ROOT/certs/ca/ca.key" \
    -CAcreateserial -days 825 -sha256 -extfile "$dir/ext.cnf" -out "$dir/server.crt" 2>/dev/null
  rm -f "$dir/server.csr"
}

# MQTT server cert：SAN 必须含 mosquitto（APP server profile 内部连 MQTT_HOST=mosquitto）/localhost/127.0.0.1/本机 hostname
echo "== 生成 MQTT bootstrap server 证书（SAN: mosquitto,localhost,u-dao,127.0.0.1）=="
sign_cert "$RUNTIME_ROOT/certs/mosquitto" mosquitto "subjectAltName=DNS:mosquitto,DNS:localhost,DNS:u-dao,IP:127.0.0.1"
cp "$RUNTIME_ROOT/certs/ca/ca.crt" "$RUNTIME_ROOT/secrets/mosquitto/certs/ca.crt"
cp "$RUNTIME_ROOT/certs/mosquitto/server.crt" "$RUNTIME_ROOT/secrets/mosquitto/certs/server.crt"
cp "$RUNTIME_ROOT/certs/mosquitto/server.key" "$RUNTIME_ROOT/secrets/mosquitto/certs/server.key"
chmod 640 "$RUNTIME_ROOT/secrets/mosquitto/certs/ca.crt" "$RUNTIME_ROOT/secrets/mosquitto/certs/server.crt" "$RUNTIME_ROOT/secrets/mosquitto/certs/server.key"

# nginx bootstrap cert：shared ingress 18443 浏览器 E2E 用
echo "== 生成 nginx bootstrap server 证书（SAN: firebot.local,localhost,u-dao,127.0.0.1）=="
sign_cert "$RUNTIME_ROOT/certs/nginx" firebot.local "subjectAltName=DNS:firebot.local,DNS:localhost,DNS:u-dao,IP:127.0.0.1"
mkdir -p "$INGRESS_TLS"
cp "$RUNTIME_ROOT/certs/nginx/server.crt" "$INGRESS_TLS/server.crt"
cp "$RUNTIME_ROOT/certs/nginx/server.key" "$INGRESS_TLS/server.key"
chmod 640 "$INGRESS_TLS/server.crt" "$INGRESS_TLS/server.key"
echo "  nginx 证书已复制到 $INGRESS_TLS"

# ---- mosquitto passwords（platform 用户）与 ACL ----
echo "== 生成 mosquitto passwords 与 acl =="
detect_docker
# mosquitto_passwd -c 拒绝覆盖已存在文件；本脚本整体重生成，先移除旧文件
rm -f "$RUNTIME_ROOT/secrets/mosquitto/passwords"
"${DOCKER[@]}" run --rm -v "$RUNTIME_ROOT/secrets/mosquitto:/work:rw" "$MOSQ_IMAGE" \
  mosquitto_passwd -b -c /work/passwords platform "$(cat "$RUNTIME_ROOT/secrets/app/mqtt_platform_password")"
# 容器内 root 生成的文件归 root：chown 回 ubuntu 后收紧权限
sudo -n chown ubuntu:ubuntu "$RUNTIME_ROOT/secrets/mosquitto/passwords" || fail "chown passwords 失败"
chmod 640 "$RUNTIME_ROOT/secrets/mosquitto/passwords"
cp "$REPO_ROOT/infra/mosquitto/acl.example" "$RUNTIME_ROOT/secrets/mosquitto/acl"
chmod 640 "$RUNTIME_ROOT/secrets/mosquitto/acl"

# ---- .env.server（来自 U-Dao 专用模板）----
echo "== 生成 .env.server =="
cp "$REPO_ROOT/deploy/u-dao/.env.u-dao.example" "$RUNTIME_ROOT/env/.env.server"
chmod 600 "$RUNTIME_ROOT/env/.env.server"
if grep -Eq 'REPLACE_|CHANGE_ME|TODO|example\.invalid' "$RUNTIME_ROOT/env/.env.server"; then
  fail "env 模板仍含占位符"
fi

# ---- BOOTSTRAP_LOCAL_ONLY 标记 ----
{
  echo "BOOTSTRAP_LOCAL_ONLY=yes"
  echo "生成时间=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "说明=本地验收 CA/证书；非公网证书；真实车辆接入前必须替换 MQTT/nginx 证书并配置正式域名。"
} > "$RUNTIME_ROOT/certs/BOOTSTRAP_LOCAL_ONLY.txt"

# ---- /etc/hosts 本地解析（可逆；正式 DNS 由 OWNER 配置后删除）----
if [ "${ADD_HOSTS}" = 1 ]; then
  if grep -q "firebot\.local" /etc/hosts; then
    echo "/etc/hosts 已含 firebot.local，跳过"
  else
    printf '\n# U-Dao bootstrap 本地解析（可逆；正式 DNS 由 OWNER 配置后删除）\n127.0.0.1 %s\n' "$HOST_ALIASES" \
      | sudo -n tee -a /etc/hosts >/dev/null || fail "写入 /etc/hosts 失败（需要 sudo）"
    echo "/etc/hosts 已添加：127.0.0.1 $HOST_ALIASES"
  fi
fi

echo "BOOTSTRAP_SECRETS=PASS runtime_root=$RUNTIME_ROOT"
