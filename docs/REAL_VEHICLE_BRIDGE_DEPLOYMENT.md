# 真实车 Vehicle Bridge 部署与验收（GitHub 为唯一交付源）

两台机器闭环：

```text
SERVER（/opt/firebot/ROBOT-Web）
  git pull → scripts/server-update.sh → scripts/server-verify.sh

VEHICLE（ROS Noetic 主机）
  git clone/pull ROBOT-Web → integration/ros1/vehicle-bridge/install.sh
  → /opt/firebot/vehicle-bridge → systemd → verify.sh
```

服务器**没有**独立 server_bridge daemon：server bridge path = `command-dispatcher` + `mqtt-ingress` + Mosquitto（`docker-compose.server.yml` 原生包含）。

---

## 1. 服务器端

```bash
cd /opt/firebot/ROBOT-Web
git fetch origin && git pull --ff-only
git checkout ui/youdao-light-hmi-v1     # approved release ref
scripts/server-update.sh                # preflight → build → migrate → recreate → health
scripts/server-verify.sh
```

- 不覆盖 `secrets/`，不重生成 MQTT 密码，不重置数据库。
- 每台真车在 `secrets/mosquitto/passwords` 加独立账号（username == canonical vehicle_id），ACL 用 `infra/mosquitto/acl.example` 的 `%u` 模板。

## 2. 车端

```bash
# 拿到代码（GitHub 唯一交付源）
git clone git@github.com:guolichen007/ROBOT-Web.git /tmp/robotweb
cd /tmp/robotweb/integration/ros1/vehicle-bridge

# 安装到固定目录（可配置）
FIREBOT_ROS_SETUP=/opt/ros/noetic/setup.bash \
FIREBOT_ROS_WORKSPACE_SETUP=/home/<ros用户>/firerobot_ws/devel/setup.bash \
./install.sh

# CA + 配置 + secret（见 vehicle-bridge/README.md）
sudo cp production-ca.crt /etc/firebot/production-ca.crt
nano /opt/firebot/vehicle-bridge/config/bridge.env
sudo install -m 600 /dev/null /etc/firebot/bridge-secret.env
echo 'FIREBOT_MQTT_PASSWORD=<密码>' | sudo tee /etc/firebot/bridge-secret.env >/dev/null

sudo systemctl enable --now firebot-bridge
./verify.sh
```

## 3. 第一轮验收矩阵

| 项 | 预期 | 备注 |
| --- | --- | --- |
| availability | 服务器看到 online | QoS1 retain |
| heartbeat | 1Hz | 车端在线判据 |
| battery | `/firebot_bridge/battery`（std_msgs/Float32）入 DB `robots.battery` | 真实源，由车端 provider/adapter 发布 |
| smoke | 仅当有真实 provider 才 PASS | Modbus/standalone，非 ROS topic；无源不发布 |
| location | 默认 disabled（`FIREBOT_LOCATION_ENABLED=false`） | 地图身份确认前不发 |
| patrol command transport | `next-cruise-loop-sender.py --once` → bridge 收到 | schema 1.3 command |
| 无 ROS 实现 | bridge 回 `rejected + BRIDGE_ADAPTER_NOT_CONNECTED` | 不 ack accepted |
| 真实运动 | 不发生 | REAL_* = NOT_IMPLEMENTED |

## 4. 边界

- 本阶段**不**做真实 ROS 控制：Bridge 只把 MQTT 命令转发到 `/firebot_bridge/command`，只有 ROS 明确回 `ACCEPTED` 才回 `accepted`。
- 所有 control flags（`control_contract_verified` 等）保持 false；`ROS_COMPAT_DOWNLINK_IMPLEMENTED` 保持 False。
- 若历史 `firebot-vehicle-01` 的 `source_kind` 仍是 `ROS_COMPAT`，用显式一次性迁移工具切换为 `CANONICAL_MQTT`（不自动、可审计、控制位强制保持 false）：
  `docker compose -f docker-compose.server.yml exec -T api python -m app.dev.set_vehicle_source_kind firebot-vehicle-01 CANONICAL_MQTT`
- 密码、生产证书私钥、现场 `.env` 不进 Git。
