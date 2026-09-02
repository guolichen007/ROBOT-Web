# 车端 Bridge 部署与实车接口

> 面向 ROS / 车端人员。现场安装与验收的唯一操作依据。
> 当前状态真相源见 [实车现场联调总览.md](实车现场联调总览.md)；技术协议见 [FIREBOT_BRIDGE_CONTRACT_1.3.md](FIREBOT_BRIDGE_CONTRACT_1.3.md)。
> 现场日常启停/状态查看见 [HANDOFF/](../integration/ros1/vehicle-bridge/HANDOFF/README.md)。

---

## 0. 当前首车现场状态

正式安装形态为 `/opt/firebot/vehicle-bridge`（install.sh 原子切换 + APPROVED_RUNTIME.txt 留痕）：

```text
VEHICLE_OS=Ubuntu 20.04
BRIDGE_INSTALL_DIR=/opt/firebot/vehicle-bridge
SYSTEMD_UNIT=/etc/systemd/system/firebot-bridge.service
ExecStart=/bin/bash /opt/firebot/vehicle-bridge/run_bridge.sh /etc/firebot/bridge.env
ROS_MASTER_URI：经 FIREBOT_ROS_SETUP / FIREBOT_ROS_WORKSPACE_SETUP 环境注入
```

当前：

```text
commands=[]
sensors=[]
location_enabled=false
CONTROL_CODE=PATROL_START,STOP_MOTION、CONTROL_FIELD_VERIFIED=NO
```

J6 支线已取代旧 `/home/tl/vehicle-bridge` Bridge-only 隔离形态；历史冻结基线 `13c8692` 见历史交接文档。
当前下一步（实车控制未开放，须按批准流程）：

```text
车端 Pull 批准 SHA → Bridge/Control 安装 → catkin_make → 静态 ROS 验证 → 再开放 stop_motion
```

> 本次任务**禁止**真实运动；`FIREBOT_SUPPORTED_COMMANDS` 不开放。

标准 `/opt/install.sh` 部署路线见下方第 2–3 节，继续保留为通用部署资产（第二台车从零部署仍按它走）。

---

## 1. Git 来源与批准基线

仓库：`guolichen007/ROBOT-Web`。车端 Bridge/Control 的**唯一源码权威**是分支
`integration/server-web-real-vehicle-ready-v1`，批准 SHA = 该分支 HEAD（不硬编码）。

**不要写“拉最新代码”**。现场安装时用明确记录的 HEAD SHA 做校验与留痕：

```bash
git fetch origin
git checkout integration/server-web-real-vehicle-ready-v1
git pull --ff-only origin integration/server-web-real-vehicle-ready-v1
git rev-parse HEAD   # 记录此 SHA，作为 FIREBOT_REQUIRE_SHA 与回滚锚点
```

> 历史冻结基线 `13c8692` / 分支 `ui/youdao-light-hmi-v1` 已由 J6 支线取代，见历史交接文档。

---

## 2. 车端固定环境

| 项 | 值 |
| --- | --- |
| OS | Ubuntu 20.04 |
| ROS | ROS Noetic |
| ROS setup | `/opt/ros/noetic/setup.bash` |
| ROS workspace setup | `/home/tl/firerobot_ws/devel/setup.bash` |
| Bridge 安装目录 | `/opt/firebot/vehicle-bridge` |
| **唯一配置文件** | `/etc/firebot/bridge.env` |
| secret | `/etc/firebot/bridge-secret.env` |
| CA | `/etc/firebot/production-ca.crt` |

现场唯一配置是 `/etc/firebot/bridge.env`（安装目录内的 `config/bridge.env.example` 只是模板，由 install.sh 复制到 `/etc/firebot/bridge.env`）；禁止部署时覆盖已有 CA。

---

## 3. 安装

```bash
cd integration/ros1/vehicle-bridge
FIREBOT_ROS_SETUP=/opt/ros/noetic/setup.bash \
FIREBOT_ROS_WORKSPACE_SETUP=/home/tl/firerobot_ws/devel/setup.bash \
./install.sh
```

安装后确认存在：

```text
/opt/firebot/vehicle-bridge/run_bridge.sh
/opt/firebot/vehicle-bridge/verify.sh
/opt/firebot/vehicle-bridge/watch-bridge.sh
/opt/firebot/vehicle-bridge/tools/field_console.py
```

任何输出都**不得**包含 secret 明文。

---

## 4. 当前安全配置（长期运行）

`/etc/firebot/bridge.env` 关键项：

```text
BRIDGE_STUB_MODE=false
FIREBOT_SUPPORTED_COMMANDS=
FIREBOT_SENSORS=
FIREBOT_LOCATION_ENABLED=false
FIREBOT_FIELD_TRACE=false
```

`FIREBOT_FIELD_TRACE=false` 是长期运行状态；R0–R4 期间的临时 `true` 已结束。

---

## 5. R0–R4 现场验收（LEGACY 历史记录，非当前下一步）

以下 R0–R4 是历史验收记录，保留技术内容仅供复验。当前 Bridge-only 周期不执行 ROS 相关项：

| 项 | 内容 | 关键约束 |
| --- | --- | --- |
| R0 | 安全 capability：看到 vehicle online、capabilities 收到、`commands=[]`、`sensors=[]` | 不声明任何真实控制能力 |
| R1 | 无 roscore：Bridge MQTT 仍在线、heartbeat 持续、`ros.master=unavailable`、adapter=not ready、boot_id/PID 不变 | 因 `supported_commands=[]`，不进行合法控制命令的 ROS 转发验证；此时发 patrol 正确结果是 `COMMAND_UNSUPPORTED` |
| R2 | Bridge 在线后启动 roscore：adapter 自动初始化，boot_id / 进程不变 | `NOT_EXECUTED_IN_CURRENT_BRIDGE_ONLY_CYCLE`（当前 ROS 被故意隔离） |
| R3 | broker 受控断开/恢复 | **OWNER_APPROVAL_REQUIRED**，车端人员不得私自重启服务器 Mosquitto |
| R4 | `MANUAL_ROSTOPIC` 手工发布 battery=67.5，验证链路到 Web | `NOT_EXECUTED_IN_CURRENT_BRIDGE_ONLY_CYCLE`；`REAL_BATTERY_PROVIDER=NOT_VERIFIED` |

R3 受控故障注入必须由 owner 批准执行；车端人员不得私自修改服务器 / 网络 / firewall。

> `BRIDGE_ADAPTER_NOT_CONNECTED` 只在“命令已通过 supported capability 校验并成功转发 ROS 后无 feedback”时才回。
> 当前 `FIREBOT_SUPPORTED_COMMANDS=`（空），R0–R4 默认安全配置下不会触发；它属于未来某命令正式加入
> `FIREBOT_SUPPORTED_COMMANDS` 之后的 adapter-loss 反证测试。

---

## 6. Field Console（现场实时控制台）

```bash
# 一次性状态快照
FIREBOT_BRIDGE_ENV=/etc/firebot/bridge.env /opt/firebot/vehicle-bridge/verify.sh

# 实时控制台（append-only）
/opt/firebot/vehicle-bridge/watch-bridge.sh
/opt/firebot/vehicle-bridge/watch-bridge.sh --verbose
/opt/firebot/vehicle-bridge/watch-bridge.sh --full-id
/opt/firebot/vehicle-bridge/watch-bridge.sh --raw
```

语义：

- `status.json` = 当前快照（启动头）；journal follow = `-n 0`，只跟随未来 transition，不重放历史事件。
- Ctrl+C 只退出 viewer，**不停止** `firebot-bridge.service`。
- 禁止 service `active` 时再手动 `run_bridge.sh`（相同 MQTT identity 会互相踢线）。

---

## 7. ROS 实车接口矩阵

Bridge → ROS（下行）：

| ROS 接口 | 类型 | Bridge 当前状态 | 等待车端什么 |
| --- | --- | --- | --- |
| `/firebot_bridge/command` | std_msgs/String (JSON) | READY | ROS 实际命令执行器 |

ROS → Bridge（上行）：

| ROS 接口 | 类型 | Bridge 当前状态 | 等待车端什么 |
| --- | --- | --- | --- |
| `/firebot_bridge/command_feedback` | std_msgs/String (JSON) | READY | 真实生命周期反馈 |
| `/firebot_bridge/battery` | std_msgs/Float32 | READY | 真实电池 provider |
| `/firebot_bridge/smoke` | std_msgs/Float32 | READY | 原始传感器 → ROS provider |
| `/firebot_bridge/status` | std_msgs/String (JSON) | READY | 真实 mode/estop_active/active_task_id |
| `/firebot_bridge/location` | std_msgs/String (JSON) | READY | 真实 map 位姿 |
| `/odom` + `/amcl_pose` | nav_msgs | READY | 可生成 location 输入 |
| `/firebot_bridge/alarm` | std_msgs/String (JSON) | **RESERVED** | Bridge adapter 后续实现，当前未接线 |

> `/firebot_bridge/alarm` 是**接口合同保留**，当前 `13c8692` 的 ros_adapter 并未订阅它，不能写成“当前实车已支持”。

---

## 8. command JSON

Bridge 发布到 `/firebot_bridge/command` 的 payload 字段：

```json
{
  "interface_version": "1.0",
  "command_id": "…",
  "task_id": "…",
  "command": "PATROL_START",
  "params": {},
  "received_at": "…",
  "expires_at": "…"
}
```

MQTT cmd → ROS command 映射（冻结）：

| MQTT cmd | ROS command |
| --- | --- |
| patrol | `PATROL_START` |
| stop_motion | `STOP_MOTION` |
| emergency_stop | `EMERGENCY_STOP` |
| reset_estop | `RESET_ESTOP` |
| return_dock | `RETURN_DOCK` |
| extinguish | `EXTINGUISH_START` |
| cancel_task | `CANCEL_TASK` |
| manual_control | `MANUAL_CONTROL` |

---

## 9. feedback

ROS 通过 `/firebot_bridge/command_feedback` 回：

```text
ACCEPTED / EXECUTING / COMPLETED / REJECTED / FAILED / CANCELLED
```

**只有 ROS 明确回 `ACCEPTED`，Bridge 才向 MQTT 回 `accepted`。** 其余状态按合同映射到 `rejected` / `task_status`。

---

## 10. 实车 provider 接法

battery：

```text
真实设备/驱动 → ROS provider → /firebot_bridge/battery → Bridge → MQTT status → Server/Web
```

smoke：

```text
Modbus / 独立传感器 / 其他车载数据源
        ↓
车端 provider（负责硬件协议转换）
        ↓
/firebot_bridge/smoke（std_msgs/Float32）
        ↓
Vehicle Bridge
        ↓
MQTT sensor
        ↓
Server / Web
```

> **Vehicle Bridge 不直接读取 Modbus。** 它对 ROS 侧的 canonical smoke 输入是 `/firebot_bridge/smoke`。

status：只允许真实 `mode` / `estop_active` / `active_task_id`。

location：可通过 canonical `/firebot_bridge/location`，或现有 `/odom + /amcl_pose` 生成；但 `FIREBOT_LOCATION_ENABLED=false` 必须保持，直到 site/map identity 正式确认。

---

## 11. capability 激活门

```text
topic 存在  !=  真实 capability 已完成
```

生产默认 `FIREBOT_SUPPORTED_COMMANDS` 必须为空。未来逐命令验证通过后再加入，**不得一次打开全部命令**。

以 patrol 为例，逐项验证顺序：

1. ROS 能订阅 `/firebot_bridge/command`
2. 正确识别 `PATROL_START`
3. ROS 自己完成真实安全检查
4. ROS 返回 `ACCEPTED`
5. 执行中返回 `EXECUTING`
6. 完成后返回 `COMPLETED`
7. 异常返回 `FAILED` / `REJECTED`
8. 验证 task lock 生命周期
9. 验证断 ROS / 重连
10. 实车安全验收 PASS
11. 最后才设置 `FIREBOT_SUPPORTED_COMMANDS=patrol`
