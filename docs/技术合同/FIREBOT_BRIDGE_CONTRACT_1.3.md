# Firebot 车端 ROS Bridge 契约 v1.3（冻结）

> 版本：1.3.0 · 2026-08-24 · 服务器 ↔ 车端 Bridge ↔ ROS 三层边界冻结
> 本轮边界：**只做 Bridge 通信层**，不实现任何真实车辆运动/巡航/急停/灭火/回充/手动控制。真实执行由车端 ROS 人员后接。

---

## 1. Canonical Identity（实测确认）

| 项 | 值 | 来源 |
|---|---|---|
| MQTT_USERNAME（车端账号） | `firebot-vehicle-01` | 车端 bridge env |
| canonical vehicle_id | `firebot-vehicle-01` | DB `robots.vehicle_id` |
| integration external_id | （当前空，不影响本轮） | `robot_integration_profiles.external_id` |
| MQTT 账号 = canonical ID | ✅ 三者一致 | — |

**结论**：全部 topic、ACL、command-dispatcher、Bridge env 统一使用 canonical ID = **`firebot-vehicle-01`**。

---

## 2. MQTT Contract（服务器 ↔ 车端 Bridge）

Broker：`100.110.31.112:8883`（TLS，项目 CA）。schema_version = `1.3`（服务器兼容 1.2/1.3）。

### 2.1 通用消息头（vehicleBase，所有上行消息必带）
```json
{ "schema_version": "1.3", "message_id": "<uuid>", "type": "<类型>",
  "vehicle_id": "firebot-vehicle-01", "boot_id": "<uuid，进程启动生成，重连不变>",
  "timestamp": "<ISO8601 UTC，车辆源时间>", "seq": <每类型递增> }
```

### 2.2 上行（车端 → 服务器，topic `robot/firebot-vehicle-01/{type}`）

| type | 频率 | 本轮 | 字段 |
|---|---|---|---|
| availability | 连接+LWT | ✅ | state(online/offline), reason；**QoS1 retain=true** |
| heartbeat | 1Hz | ✅ | uptime_seconds |
| capabilities | 连接时 | ✅ | protocol_version=1.3, **supported_commands=真实能力**, **sensors=[真实传感器]**（唯一权威）, media；**QoS1 retain=true** |
| status | 1Hz | ✅ | **partial**：mode/battery/estop_active/active_task_id 均可缺失，只发真实字段（`{"battery":82.4}` 合法） |
| sensor | 1Hz | ✅ | **capability-driven：smoke / bottom_ir / top_ir_max 至少一个**，有则带（缺的不出现，不伪造 0） |
| location | ≤10Hz | 冻结 | position(x/y/theta, map 系), linear_speed, angular_speed, battery, site_code, map_code, map_version, map_checksum, frame_id=map |
| command_ack | 命令后即回 | ✅ | command_id, **task_id（顶层）**, status(accepted/rejected/unsupported), reason_code(枚举白名单) |
| task_status | 任务事件 | 冻结 | task_id, status, phase, progress, failure_code/message, checkpoint/waypoint 游标 |
| alarm | 火警 | 冻结 | event_id, fire_type, severity, position, ... |

### 2.3 下行（服务器 → 车端，topic `robot/firebot-vehicle-01/command`）

schema 1.3 command。**task_id 用顶层字段**；params 只放命令自身参数。

cmd 枚举：`patrol / stop_motion / emergency_stop / reset_estop / return_dock / extinguish / cancel_task / manual_control`

**禁止** `START` 等私有协议。

---

## 3. ROS Placeholder Contract（车端 Bridge ↔ ROS，ROS 人员只接这一侧）

统一前缀 `/firebot_bridge/`。ROS 人员不修改 MQTT 层，只实现下列接口。

### 3.1 下行（Bridge → ROS）：`/firebot_bridge/command`（std_msgs/String, JSON）
```json
{ "interface_version": "1.0",
  "command_id": "C2026...", "task_id": "...",
  "command": "PATROL_START",
  "params": {},
  "received_at": "2026-08-24T...Z", "expires_at": "2026-08-24T...+30s" }
```
`command` 枚举：
| MQTT cmd | ROS command |
|---|---|
| patrol | `PATROL_START` |
| stop_motion | `STOP_MOTION` |
| emergency_stop | `EMERGENCY_STOP` |
| reset_estop | `RESET_ESTOP` |
| return_dock | `RETURN_DOCK` |
| extinguish | `EXTINGUISH_START` |
| cancel_task | `CANCEL_TASK` |
| manual_control | `MANUAL_CONTROL` |

### 3.2 上行（ROS → Bridge）：`/firebot_bridge/command_feedback`（JSON）
```json
{ "command_id": "...", "task_id": "...",
  "state": "ACCEPTED|EXECUTING|COMPLETED|REJECTED|FAILED|CANCELLED",
  "reason_code": null, "message": null, "progress": 0, "phase": "PATROLLING" }
```
- ROS 回 `ACCEPTED` → Bridge 回 MQTT `command_ack accepted`；任务命令（patrol/return_dock/extinguish）同时发 `task_status=accepted`
- ROS 回 `REJECTED`/`FAILED` → Bridge 回 MQTT `rejected`（带 reason_code）
- `EXECUTING`/`COMPLETED`/`CANCELLED` → 任务命令发 MQTT `task_status`（executing/completed/cancelled）；非任务命令不产生 task_status

### 3.3 数据上行（ROS → Bridge）
| topic | 类型 | 说明 |
|---|---|---|
| `/firebot_bridge/battery` | std_msgs/Float32 | 电量百分比（canonical 唯一来源） |
| `/firebot_bridge/smoke` | std_msgs/Float32 | 烟雾浓度（**canonical ROS 输入**；原始硬件可为 Modbus/standalone，由车端 provider 完成硬件协议→ROS 转换） |
| `/firebot_bridge/status` | std_msgs/String(JSON) | mode/estop_active/active_task_id |
| `/firebot_bridge/location` | std_msgs/String(JSON) | x/y/theta/linear/angular（map 系）；亦由 `/odom` + `/amcl_pose` 生成 |
| `/firebot_bridge/alarm` | std_msgs/String(JSON) | 火警（接口合同保留；**当前 13c8692 ros_adapter 未接入，CURRENT_STATUS=RESERVED**） |

> alarm 是接口常量保留，不是当前已接线：`13c8692` 的 ros_adapter 未建立 `/firebot_bridge/alarm` subscriber，现场不能写“已支持”。

### 3.4 Bridge 固定流程
```
MQTT command → 校验(boot/过期/支持性) → 去重(command_id 幂等)
  → 发布 /firebot_bridge/command
  → 订阅 /firebot_bridge/command_feedback
  → 据反馈发 MQTT command_ack / task_status
```

### 3.5 占位语义（生产默认）
- `BRIDGE_STUB_MODE=false`（生产）：Bridge 转发命令到 ROS placeholder；无 ROS feedback → 回 MQTT `rejected` + `BRIDGE_ADAPTER_NOT_CONNECTED`。**绝不 ack accepted、绝不调用任何本地执行函数（含 emergency_stop）**。
- `BRIDGE_STUB_MODE=true`（联调）：测试适配器可临时声明命令、可模拟 feedback，仅供消息闭环联调。
- `COMMAND_UNSUPPORTED` 保留给"协议不认识/不支持"的命令；`BRIDGE_ADAPTER_NOT_CONNECTED` 用于"协议合法但 ROS 未实现"。

---

## 4. 数据源映射（本轮）

| 数据 | 真实来源 | Bridge 处理 |
|---|---|---|
| battery | `/firebot_bridge/battery`（std_msgs/Float32，由车端 provider/adapter 发布） | status partial `{"battery": ...}` |
| smoke | 原始硬件（Modbus/standalone/其他车载数据源）→ 车端 provider → `/firebot_bridge/smoke`（canonical ROS 输入） | 有真实源才发 sensor `{"smoke": ...}`；无源**不发布 sensor**，不伪造 0；Bridge 不直接读 Modbus |
| mode/estop | 车端未提供前**不伪造** | status 缺省，服务器保持 NULL/unknown |
| location | canonical `/firebot_bridge/location`，或 `/odom`(速度) + `/amcl_pose`(map 位姿) | 以 amcl/map 为准；对外上行仍受 `FIREBOT_LOCATION_ENABLED` 控制，确认前不发 |

---

## 5. 关键行为约束

- **command_id 幂等**：QoS1 重复投递返回之前结果，不重复发布 ROS command。
- **boot_id 生命周期**：进程启动生成新 uuid；MQTT 重连不变；进程重启才变。服务器 RobotBootSession 按此管理。
- **LWT**：offline 不按 seq 单调处理（payload 连接前固定）；用 message_id/服务器接收时间去重时序；正常重连不使同 boot_id 失效。
- **location**：以 amcl/map 为准；每次发布走新 seq；显式节流 ≥100ms。
- **ack reason_code 枚举白名单**：`ROBOT_BOOT_SESSION_UNKNOWN / ACTIVE_TASK_CONFLICT / COMMAND_EXPIRED / COMMAND_UNSUPPORTED / ROBOT_ESTOP_ACTIVE / BRIDGE_ADAPTER_NOT_CONNECTED`（后两者需 v1.3 枚举支持）。

## 6. 安全边界
- 本轮零真实运动；所有命令（含 estop）只转发 ROS placeholder，生产不执行、不 ack accepted。
- 生产不 sleep 模拟任务成功；`BRIDGE_STUB_MODE` 仅联调临时开启。
- `capabilities.supported_commands` 只声明真实能力；占位接口不宣称 supported。
- 真车最终防线：本地物理急停 + LWT 离线。
