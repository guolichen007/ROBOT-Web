# Ubuntu SERVER 从零部署

> ⚠️ **注意**：本文仅用于「**新服务器从零部署**」。
> 当前已经运行的 Firebot 现场服务器，**不得**因为 Vehicle Bridge R0–R4 验证而按本文执行 checkout / update。
> 当前现场服务器操作请使用 [服务器与Web现场配合.md](服务器与Web现场配合.md)。

本说明以 Ubuntu Server 24.04 LTS、Docker Engine 与 Compose plugin 为推荐主线。执行结果只能声明 `SERVER_DEPLOYMENT_READY`；只有在目标第二台服务器真实完成后才能声明 deployed。

## 1. 系统与 Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl mosquitto-clients chrony
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo tee /etc/apt/keyrings/docker.asc >/dev/null
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker chrony
```

## 2. Clone 与 tag

```bash
sudo mkdir -p /opt/firebot
sudo chown "$USER":"$USER" /opt/firebot
git clone git@github.com:guolichen007/ROBOT-Web.git /opt/firebot/ROBOT-Web
cd /opt/firebot/ROBOT-Web
git checkout ui/youdao-light-hmi-v1    # approved release ref（不再引用历史 tag v2.0.0-integration-ready）
cp .env.server.example .env.server
```

把 `.env.server` 中 `firebot-server.example.invalid` 改为正式 hostname；保留 `*_FILE` 路径，不把 secret 明文写入 env。

## 3. Secret 目录

```bash
install -d -m 700 secrets/{app,postgres,redis,mosquitto/certs,nginx}
umask 077
openssl rand -base64 48 > secrets/app/jwt_secret
openssl rand -base64 48 > secrets/app/refresh_secret
openssl rand -base64 48 > secrets/app/csrf_secret
openssl rand -base64 32 > secrets/app/bootstrap_admin_password
openssl rand -base64 36 > secrets/app/mqtt_platform_password
openssl rand -base64 36 > secrets/app/media_publish_token
openssl rand -base64 32 > secrets/postgres/password
openssl rand -base64 32 > secrets/redis/password
printf 'postgresql+psycopg://firebot:%s@postgres:5432/firebot' "$(cat secrets/postgres/password)" > secrets/app/database_url
printf 'redis://:%s@redis:6379/0' "$(cat secrets/redis/password)" > secrets/app/redis_url
chmod 600 secrets/app/* secrets/postgres/password secrets/redis/password
```

## 4. MQTT CA、证书、password 与 ACL

使用部署所有者签发的正式 CA/server certificate；证书 SAN 必须包含 MQTT hostname。文件放置：

```text
secrets/mosquitto/certs/ca.crt
secrets/mosquitto/certs/server.crt
secrets/mosquitto/certs/server.key
secrets/mosquitto/passwords
secrets/mosquitto/acl
```

生成 password file：

```bash
docker run --rm -v "$PWD/secrets/mosquitto:/work" eclipse-mosquitto:2.1.2-alpine \
  mosquitto_passwd -b -c /work/passwords platform "$(cat secrets/app/mqtt_platform_password)"
cp infra/mosquitto/acl.example secrets/mosquitto/acl
chmod 600 secrets/mosquitto/passwords secrets/mosquitto/acl secrets/mosquitto/certs/server.key
```

每台真车必须使用独立用户名和只读自己 command/只写自己 telemetry 的 ACL。

## 5. Nginx TLS、preflight 与启动

将正式 HTTPS `server.crt/server.key` 放入 `secrets/nginx/`，权限 600。

```bash
mkdir -p backups
chmod +x scripts/server-preflight.sh
SERVER_ENV_FILE=.env.server scripts/server-preflight.sh
docker compose --env-file .env.server -f docker-compose.server.yml build
docker compose --env-file .env.server -f docker-compose.server.yml up -d --wait
docker compose --env-file .env.server -f docker-compose.server.yml exec -T api alembic current
```

bootstrap admin secret 不写日志；部署所有者从受控 secret 文件取得首次密码，登录后立即修改并撤销 bootstrap 文件访问。

## 6. Firewall 与验证

```bash
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 8883/tcp
sudo ufw enable
curl -fsS https://YOUR_HOST/health/live
mosquitto_sub -h YOUR_HOST -p 8883 --cafile ca.crt -u firebot-vehicle-01 -P 'OWNER_SECRET' -t 'robot/firebot-vehicle-01/command' -d
```

公网不得开放 5432、6379、1883、8554、8889、9997、metrics 或 ready。浏览器确认登录、Settings、media ticket；机器人先按 ROS2 handoff Gate 验证 identity/heartbeat/location，再验证命令。

## 7. 备份、重启、更新与回滚

备份计划调用服务器适配后的 pg_dump/assets/config 脚本，备份目标必须独立磁盘或受控远端。RPO/RTO 由 owner 决定。

```bash
docker compose --env-file .env.server -f docker-compose.server.yml restart
docker compose --env-file .env.server -f docker-compose.server.yml logs --since 30m
docker compose --env-file .env.server -f docker-compose.server.yml down
```

更新前备份，checkout 已批准 ref（`ui/youdao-light-hmi-v1`），然后运行 `scripts/server-update.sh`（preflight → build → migrate → recreate 变更服务 → health）。回滚 checkout 上一 ref 并恢复兼容备份；不得 force reset 主线或盲目降级数据库。

所有 `YOUR_HOST`、证书和现场账号都必须由 deployment owner 提供；文档中的示例不是生产值。
