# Firebot 车端 ROS Bridge（通信层 · schema 1.3）

> 版本：1.3.0 · 2026-08-24
> 边界：**只做 Bridge 通信层**。不实现任何真实车辆运动/巡航/急停/灭火/回充/手动控制。真实执行由车端 ROS 控制程序后续实现。
> 协议契约见上级目录 `next-vehicle-bridge-contract-1.3.md`。

## 职责

```
服务器 Web ── MQTT/TLS ── 本 Bridge ── /firebot_bridge/* ── ROS 控制程序(后续)
     上行: battery/smoke/heartbeat/...      下行: patrol/stop/estop/...
```

Bridge 只做 5 件事：MQTT 连接、协议校验、上行数据封装、下行命令转交、ROS 反馈重新封装。
**它不知道"怎么巡航"**——只把"服务器要求开始巡航"可靠转交给 ROS 层。

## 目录结构

```
ros-bridge/
├── firebot_bridge/
│   ├── main.py            # 入口（线程布局）
│   ├── config.py          # env 配置
│   ├── mqtt_client.py     # MQTTv5+TLS+LWT+reconnect+订阅 command
│   ├── protocol.py        # vehicleBase/seq/消息构造/命令校验（兼容 1.2/1.3）
│   ├── identity.py        # boot_id 生命周期
│   ├── state.py           # 共享状态（任务锁/数据缓存/幂等）
│   ├── uplink/            # 上行消息：availability/heartbeat/capabilities/status/sensor/location
│   ├── downlink/          # 下行：command_receiver/validator/dedup/ros_placeholder
│   └── ros/               # /firebot_bridge/* 冻结契约：interfaces/providers/feedback
├── config/bridge.env.example   # 配置模板
├── run_bridge.sh               # 启动（systemd 修复版）
├── firebot-bridge.service      # systemd（EnvironmentFile 注入密码）
├── tests/test_bridge.py        # 协议/幂等/partial 单测
└── README.md
```

## 部署（车端 tl-RaptorLake）

```bash
# 1) 依赖
pip3 install paho-mqtt          # rospy 随 ROS Noetic 自带

# 2) 拷贝整个 ros-bridge/ 到车端，例如 /home/tl/firebot-bridge/

# 3) CA 放好
sudo cp production-ca.crt /etc/firebot/production-ca.crt

# 4) 配置（无密码）
cp config/bridge.env.example config/bridge.env
nano config/bridge.env   # 按需改 SITE_CODE/MAP/频率；密码不写这里

# 5) 密码（二选一，优先 EnvironmentFile）
sudo mkdir -p /etc/firebot
# 方式A（systemd EnvironmentFile，推荐）：
sudo tee /etc/firebot/firebot-bridge.env >/dev/null <<'EOF'
FIREBOT_MQTT_PASSWORD=<车辆MQTT密码>
EOF
sudo chmod 600 /etc/firebot/firebot-bridge.env
# 方式B（旧安全文件）：
echo -n '<车辆MQTT密码>' | sudo tee /etc/firebot/vehicle-mqtt-password >/dev/null
sudo chmod 600 /etc/firebot/vehicle-mqtt-password

# 6) systemd 常驻
sudo cp firebot-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now firebot-bridge

# 7) 验证常驻
systemctl status firebot-bridge          # active
journalctl -u firebot-bridge -f          # 看 MQTT connected + heartbeat
```

## 关键配置（config/bridge.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `FIREBOT_MQTT_USERNAME` | firebot-vehicle-01 | 车端账号（canonical ID） |
| `FIREBOT_VEHICLE_ID` | firebot-vehicle-01 | canonical vehicle_id |
| `BRIDGE_STUB_MODE` | false | **生产必须 false**；true=联调 |
| `FIREBOT_SUPPORTED_COMMANDS` | 空 | 只声明真实能力；未接 ROS 不声明 |
| `FIREBOT_SENSORS` | 空 | 唯一权威传感器声明；有真实 smoke 源才声明 `smoke` |
| `FIREBOT_LOCATION_ENABLED` | false | location 上行门控；地图身份真实确认前保持 false |
| `FIREBOT_FEEDBACK_TIMEOUT_SECONDS` | 3 | 无 ROS feedback 时回 rejected 的超时 |

## 命令处理语义（重要）

- 生产模式：收到命令 → 校验/去重 → 转发 `/firebot_bridge/command` → 等 ROS `command_feedback`。
  **只有 ROS 明确回 ACCEPTED 后 Bridge 才回 MQTT accepted**；超时无 feedback → `rejected` + `BRIDGE_ADAPTER_NOT_CONNECTED`。
- 所有命令（含 emergency_stop）只转发，**不做任何执行**。
- `BRIDGE_STUB_MODE=true`（联调）：可临时声明测试命令、模拟 feedback，仅供消息闭环联调。

## 测试

```bash
cd ros-bridge && python3 tests/test_bridge.py
```

## 验收边界

本轮正确结果：`REAL_PATROL/REAL_STOP/REAL_ESTOP/REAL_EXTINGUISH/REAL_MANUAL_CONTROL = NOT_IMPLEMENTED`。
绝不因 MQTT 命令到达就标记"真实控制已验证"。
