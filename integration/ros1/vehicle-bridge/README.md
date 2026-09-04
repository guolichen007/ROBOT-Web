# Firebot 车端 ROS Bridge（通信层 · schema 1.3）

> 版本：1.3.0 · 2026-08-24
> 边界：**只做 Bridge 通信层**。不实现任何真实车辆运动/巡航/急停/灭火/回充/手动控制。真实执行由车端 ROS 控制程序后续实现。
> 协议契约见 [`docs/FIREBOT_BRIDGE_CONTRACT_1.3.md`](../../../docs/技术合同/FIREBOT_BRIDGE_CONTRACT_1.3.md)。

## 当前首车现场状态

```text
批准基线     = baseline/server-runtime-2026-09-03（不可变运行基线 tag）
正式安装目录 = /opt/firebot/vehicle-bridge（install.sh 原子切换 + APPROVED_RUNTIME.txt 留痕）
运行方式     = systemd firebot-bridge.service → bash run_bridge.sh（ROS 路径经环境变量）

当前安全态：commands=[]、sensors=[]、location_enabled=false
控制能力   ：CONTROL_CODE=PATROL_START,STOP_MOTION、CONTROL_FIELD_VERIFIED=NO
当前 Gate  ：Bridge communication/operation、broker reconnect、graceful stop、LWT、
          systemd recovery、short soak 均 PASS；long soak DEFERRED；Web UI NOT_CHECKED。

现场日常启停/状态/实时控制台见 [车端部署与实车接口](../../../docs/部署运维/车端部署与实车接口.md)；当前状态真相源见 [当前状态](../../../docs/当前状态/当前状态.md)。
```

## 职责

```
服务器 Web ── MQTT/TLS ── 本 Bridge ── /firebot_bridge/* ── ROS 控制程序(后续)
     上行: battery/smoke/heartbeat/...      下行: patrol/stop/estop/...
```

Bridge 只做 5 件事：MQTT 连接、协议校验、上行数据封装、下行命令转交、ROS 反馈重新封装。
**它不知道"怎么巡航"**——只把"服务器要求开始巡航"可靠转交给 ROS 层。

**MQTT 与 ROS master 解耦**：MQTT/TLS 生命周期不依赖 roscore——无 roscore 时 MQTT 仍在线；
production 默认 `supported_commands=[]`，此时任何命令都在 validator 回 `COMMAND_UNSUPPORTED`
（不会转发 ROS）；只有命令已通过 capability 校验并成功转发 ROS 后无 feedback，才回 `BRIDGE_ADAPTER_NOT_CONNECTED`。
初始 MQTT 连接失败在进程内指数退避重试，不靠 systemd 重启。

**STANDARD_RUNTIME_BEHAVIOR**：真实 ROS_MASTER_URI 可达时 adapter 可自动初始化。

**CURRENT_FIRST_VEHICLE**：`ROS_MASTER_URI=http://127.0.0.1:1`，因此即使真实 roscore 存在，
当前 Bridge 也故意不会自动连接；进入 Phase E1 前不得改变。

**battery canonical 来源**：`/firebot_bridge/battery`（std_msgs/Float32），由车端 provider/adapter 发布；
没有 provider 时不伪造电量（`BATTERY_PROVIDER=NOT_AVAILABLE`）。

## 目录结构

```
vehicle-bridge/
├── firebot_bridge/
│   ├── main.py            # 入口（线程布局）
│   ├── config.py          # env 配置
│   ├── mqtt_client.py     # MQTTv5+TLS+LWT+reconnect+订阅 command
│   ├── protocol.py        # vehicleBase/seq/消息构造/命令校验（下行接受 1.2/1.3）
│   ├── identity.py        # boot_id 生命周期
│   ├── state.py           # 共享状态（任务锁/数据缓存/命令幂等重放）
│   ├── field_trace.py     # FBTRACE 结构化事件（journal，无 ANSI）
│   ├── uplink/            # 上行消息：availability/heartbeat/capabilities/status/sensor/location
│   ├── downlink/          # 下行：command_receiver/validator/dedup/ros_placeholder
│   └── ros/               # /firebot_bridge/* 冻结契约：interfaces/providers/feedback/ros_types
├── config/bridge.env.example        # 配置模板（无密码、无危险地图默认值）
├── systemd/firebot-bridge.service.template  # systemd 模板（install.sh 生成实际 unit）
├── install.sh / uninstall.sh / verify.sh    # 安装 / 卸载 / 验收
├── run_bridge.sh               # 启动（ROS 路径经环境变量，不硬编码 /home/tl）
├── watch-bridge.sh             # 现场实时控制台（观察者）
├── tools/field_console.py      # 终端 viewer（渲染 FBTRACE，只观察不改状态）
├── requirements.txt            # paho-mqtt
├── tests/                      # test_bridge / test_reliability / test_field_console
└── README.md
```

## 部署（车端，GitHub 为唯一交付源）

默认安装到固定目录 `/opt/firebot/vehicle-bridge`，不绑定开发人员 home；systemd 由 install.sh 从模板生成。

```bash
# 1) 依赖
pip3 install -r requirements.txt          # paho-mqtt；rospy 随 ROS Noetic 自带

# 2) 安装（可配置：FIREBOT_INSTALL_DIR / FIREBOT_BRIDGE_USER / FIREBOT_ROS_SETUP / FIREBOT_ROS_WORKSPACE_SETUP）
./install.sh

# 3) 确认 CA 存在 + 编辑配置（无密码；唯一配置 = /etc/firebot/bridge.env）
test -f /etc/firebot/production-ca.crt || { echo "STOP: CA 不存在，向部署所有者索取"; exit 1; }
nano /etc/firebot/bridge.env   # SITE/MAP/频率/STUB；密码不写这里

# 4) secret：已有则保留，绝不覆盖/重新生成；缺失则 STOP，由部署所有者提供
if sudo test -f /etc/firebot/bridge-secret.env; then
  echo "PASS: existing bridge secret preserved"
else
  echo "STOP: bridge secret missing; deployment owner must provision it"
  exit 1
fi
sudo stat -c '%U:%G %a %n' /etc/firebot/bridge-secret.env

# 5) 启动
sudo systemctl enable --now firebot-bridge

# 6) 验收
./verify.sh
```

卸载：`./uninstall.sh`（`--purge` 删除安装目录；secret 永不删除）。

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
cd vehicle-bridge && python3 tests/test_bridge.py
```

## 现场实时通信控制台

Bridge 输出无 ANSI 的结构化 `FBTRACE` 事件（journal），终端渲染由独立 viewer 完成。
现场人员不需要懂 MQTT / rospy / Python，只看 `● ○ × RX TX` 就能判断信号卡在哪一段链路。

```bash
# 状态快照（一次性）
FIREBOT_BRIDGE_ENV=/etc/firebot/bridge.env /opt/firebot/vehicle-bridge/verify.sh

# 实时控制台（append-only，SSH 友好）
/opt/firebot/vehicle-bridge/watch-bridge.sh

# verbose / full id / raw
/opt/firebot/vehicle-bridge/watch-bridge.sh --verbose
/opt/firebot/vehicle-bridge/watch-bridge.sh --full-id
/opt/firebot/vehicle-bridge/watch-bridge.sh --raw
```

现场 R0–R4 验证期间临时开启 trace（正常长期运行保持 false）：

```text
FIREBOT_FIELD_TRACE=true
```

`watch-bridge.sh` 只是观察者：Ctrl+C 只退出 viewer，**不会**停止 `firebot-bridge.service`。

现场部署与实车操作见 [车端部署与实车接口](../../../docs/部署运维/车端部署与实车接口.md)；当前状态真相源见 [当前状态](../../../docs/当前状态/当前状态.md)。

## ⚠️ 禁止双 Bridge

不要在 `firebot-bridge` service 处于 `active` 时再手动运行 `./run_bridge.sh`——相同的
MQTT client identity 会互相踢线。如确需 foreground 调试：

```bash
sudo systemctl stop firebot-bridge
systemctl is-active firebot-bridge   # 确认不是 active 后
./run_bridge.sh
# 调试结束：
sudo systemctl start firebot-bridge
```

## 验收边界

本轮正确结果：`PATROL_START`/`STOP_MOTION` 代码已实现（`CONTROL_CODE`），但 `CONTROL_FIELD_VERIFIED=NO`；`emergency_stop`/`reset_estop` 未实现。
绝不因 MQTT 命令到达就标记"真实控制已验证"。
