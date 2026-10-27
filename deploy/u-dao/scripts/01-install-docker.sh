#!/usr/bin/env bash
# U-Dao Docker 底座安装：官方 apt 源 + data-root/containerd root 落 /srv/u-dao
# + 避开腾讯云 VPC 的网络规划 + fail-closed 挂载守卫 + 日志磁盘治理 + major-version gate。
# 用法：sudo bash deploy/u-dao/scripts/01-install-docker.sh
set -euo pipefail

fail() { echo "DOCKER_INSTALL=FAIL: $1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "must run as root (sudo)"

U_DAO_ROOT=/srv/u-dao
findmnt "$U_DAO_ROOT" >/dev/null 2>&1 || fail "DATA_DISK_PRECHECK=FAIL: $U_DAO_ROOT 未挂载（先执行 00-prepare-data-disk.sh）"

# ---- 0) 网络冲突复核（与腾讯云 VPC 172.17.0.0/20 无重叠的 RFC1918 段）----
DOCKER0_CIDR="10.239.0.0/24"
POOL_CIDR="10.240.0.0/16"
check_conflict() {
  local net="$1" what="$2"
  if ip -4 route show | grep -qE "^$net([[:space:]]|$)"; then
    echo "DOCKER_ADDRESS_POOL_PRECHECK=FAIL: $what $net 与既有路由冲突："
    ip -4 route show | grep -E "^$net([[:space:]]|$)" || true
    exit 1
  fi
  if ip -4 addr show | grep -qE "inet $net([[:space:]]|$)"; then
    echo "DOCKER_ADDRESS_POOL_PRECHECK=FAIL: $what $net 与本机地址冲突"
    exit 1
  fi
}
check_conflict "$DOCKER0_CIDR" "docker0-bip"
check_conflict "$POOL_CIDR" "compose-address-pool"
echo "DOCKER_ADDRESS_POOL_PRECHECK=PASS docker0=$DOCKER0_CIDR pool=$POOL_CIDR"

# ---- 1) 官方 apt 源（幂等：GPG key 已存在则跳过下载，重试 3 次应对瞬态网络）----
apt-get update -qq
apt-get install -y -qq ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
if [ -s /etc/apt/keyrings/docker.asc ]; then
  echo "docker GPG key 已存在，跳过下载"
else
  for attempt in 1 2 3; do
    if curl -fsSL --retry 2 --retry-delay 2 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc; then
      break
    fi
    echo "GPG key 下载失败（第 $attempt 次）"
    [ "$attempt" = 3 ] && fail "GPG key 下载 3 次失败"
    sleep 3
  done
fi
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq

# ---- 2) 安装（不装 docker.io；已装则跳过安装步骤）----
if command -v docker >/dev/null 2>&1; then
  echo "docker 已安装：$(docker --version)"
else
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

# ---- 3) major-version gate（2026-10 U-Dao 审计基线；不同 major 需重新审查后放行）----
# 审查记录（2026-10-27 安装实测）：
#   docker-ce 29.9.2：全新安装默认启用 containerd image store。
#   docker-compose-plugin 5.6.1：compose v5 为当前官方主线；依赖的两项关键语义
#     （ports 四元组合并、!override/!reset 整属性替换）在 v5 保留，由 test_compose_render 实证验证。
EXPECTED_DOCKER_MAJOR=29   # 29 起全新安装默认启用 containerd image store
EXPECTED_COMPOSE_MAJOR=5
docker_major="$(docker --version | sed -E 's/.*version ([0-9]+)\..*/\1/')"
compose_major="$(docker compose version | sed -E 's/.*version v?([0-9]+)\..*/\1/')"
echo "docker_major=$docker_major compose_major=$compose_major"
[ "$docker_major" = "$EXPECTED_DOCKER_MAJOR" ] \
  || fail "docker major=$docker_major 与审计基线 $EXPECTED_DOCKER_MAJOR 不符，人工审查后再放行"
[ "$compose_major" = "$EXPECTED_COMPOSE_MAJOR" ] \
  || fail "compose major=$compose_major 与审计基线 $EXPECTED_COMPOSE_MAJOR 不符，人工审查后再放行"
docker --version
docker compose version

# ---- 4) daemon.json：data-root / bip / address-pools / local 日志驱动 + rotation ----
install -m 0755 -d /etc/docker
cat > /etc/docker/daemon.json <<EOF
{
  "data-root": "$U_DAO_ROOT/platform/docker",
  "bip": "10.239.0.1/24",
  "default-address-pools": [
    { "base": "10.240.0.0/16", "size": 24 }
  ],
  "log-driver": "local",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "registry-mirrors": ["https://mirror.ccs.tencentyun.com"]
}
EOF
# 注：registry-mirrors 使用腾讯云官方镜像源——Docker Hub 在中国大陆网络不可直连（2026-10 实测 i/o timeout）。
# OWNER 若配置网络代理可移除该镜像配置。

# ---- 5) containerd root（Docker 29 全新安装默认 containerd image store，镜像/快照实际写 containerd root）----
if [ -f /etc/containerd/config.toml ]; then
  if ! grep -q '^root[[:space:]]*=' /etc/containerd/config.toml; then
    cp /etc/containerd/config.toml "/etc/containerd/config.toml.bak.$(date +%Y%m%dT%H%M%SZ)"
    printf '\nroot = "%s/platform/containerd"\n' "$U_DAO_ROOT" >> /etc/containerd/config.toml
  fi
else
  install -m 0755 -d /etc/containerd
  printf 'root = "%s/platform/containerd"\n' "$U_DAO_ROOT" > /etc/containerd/config.toml
fi
chown -R root:root "$U_DAO_ROOT/platform/docker" "$U_DAO_ROOT/platform/containerd" 2>/dev/null || true

# ---- 6) systemd fail-closed：/srv/u-dao 未挂载 → docker/containerd 不得启动 ----
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/u-dao-mount-guard.conf <<EOF
[Unit]
RequiresMountsFor=$U_DAO_ROOT
ConditionPathIsMountPoint=$U_DAO_ROOT
EOF
if systemctl list-unit-files containerd.service >/dev/null 2>&1; then
  mkdir -p /etc/systemd/system/containerd.service.d
  cat > /etc/systemd/system/containerd.service.d/u-dao-mount-guard.conf <<EOF
[Unit]
RequiresMountsFor=$U_DAO_ROOT
ConditionPathIsMountPoint=$U_DAO_ROOT
EOF
fi
systemctl daemon-reload

# ---- 7) 启动与实测 ----
systemctl enable --now docker 2>/dev/null || systemctl enable --now docker
systemctl enable --now containerd 2>/dev/null || true
systemctl restart docker
sleep 3
echo "== docker info 实测 =="
docker info 2>/dev/null | grep -E "Docker Root Dir|Storage Driver|Logging Driver|Containerd|Default Address Pools" || true
echo "DriverStatus=$(docker info -f '{{.DriverStatus}}' 2>/dev/null)"
echo "== containerd root =="
grep -E '^root' /etc/containerd/config.toml || true
echo "DOCKER_INSTALL=PASS data_root=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)"
