# Firebot 车端控制交接方案（车端 Bridge ↔ 车端控制层）

> 状态：服务器 → MQTT → 车端 Bridge → ROS 的**下行信号链路已打通并验证**（patrol 命令已到达车端终端 `/firebot_bridge/command`）。
> 本方案面向**车端控制团队**，说明控制程序如何与 Bridge 对接。Bridge 只做通信转发，**不执行任何运动**；实际控制由车端控制层负责。
> 约束：本阶段车端只接通信与控制层交接，Bridge 代码不改；所有控制执行由车端控制团队自行实现并自担安全责任。

---

## 一、交接定位（一句话）

Bridge 是「协议翻译 + 安全网关」，车端控制团队只需要做一件事：**写一个 ROS 控制 adapter**，订阅下行 `/firebot_bridge/command`，执行真实控制，并把执行状态回发到上行 `/firebot_bridge/command_feedback`；同时把电量/烟雾/状态等遥测发布到固定 topic。

---

## 二、ROS 接口契约（冻结，见 `integration/ros1/vehicle-bridge/firebot_bridge/ros/interfaces.py`）

ROS 命名空间统一为 `/firebot_bridge`。

### 2.1 下行（Bridge → 车端控制层）——车端订阅

| topic | 类型 | 说明 |
|---|---|---|
| `/firebot_bridge/command` | `std_msgs/String`（JSON） | 服务器下发的控制命令 |

### 2.2 上行（车端控制层 → Bridge）——车端发布

| topic | 类型 | 说明 |
|---|---|---|
| `/firebot_bridge/command_feedback` | `std_msgs/String`（JSON） | 命令执行反馈（**闭环关键**） |
| `/firebot_bridge/battery` | `std_msgs/Float32` | 电量百分比 |
| `/firebot_bridge/smoke` | `std_msgs/Float32` | 烟雾值 |
| `/firebot_bridge/status` | `std_msgs/String`（JSON） | 车端模式 / 急停 / 当前任务 |
| `/firebot_bridge/location` | `std_msgs/String`（JSON） | 定位（需开启 location 才上行） |
| `/firebot_bridge/alarm` | 定义但**本轮未接线** | 火警告警（占位，后续接） |

---

## 三、下行命令契约（控制层要解析的内容）

`/firebot_bridge/command` 的 JSON 载荷（`build_ros_command` 生成）：

```json
{
  "interface_version": "1.0",
  "command_id": "fe190406-7976-4b65-92c7-72b9bba477a1",
  "task_id": "5fbd2962-39eb-4b8f-a55c-19c2773fc432",
  "command": "PATROL_START",
  "params": {},
  "received_at": "2026-08-24T06:58:27.189687+00:00",
  "expires_at": "2026-08-24T06:58:57.189687+00:00"
}
```

**字段说明：**
- `command_id`：本次命令唯一 ID，反馈时必须原样回传。
- `task_id`：任务类命令非空；非任务类命令可能为空。
- `command`：标准化后的 ROS 命令名（见下表）。
- `params`：命令参数（当前 patrol 为空对象，后续扩展）。
- `expires_at`：过期时间，控制层**不应执行已过期命令**。

**MQTT cmd → ROS command 映射（冻结，`interfaces.py`）：**

| 服务器 MQTT cmd | ROS command |
|---|---|
| `patrol` | `PATROL_START` |
| `stop_motion` | `STOP_MOTION` |
| `emergency_stop` | `EMERGENCY_STOP` |
| `reset_estop` | `RESET_ESTOP` |
| `return_dock` | `RETURN_DOCK` |
| `extinguish` | `EXTINGUISH_START` |
| `cancel_task` | `CANCEL_TASK` |
| `manual_control` | `MANUAL_CONTROL` |

**任务类 vs 非任务类（控制层要区分）：**
- 任务类：`PATROL_START`、`RETURN_DOCK`、`EXTINGUISH_START` —— 独占任务锁，会产生 task_status。
- 非任务类：其余 —— 只有 accepted/rejected 语义，**不产生 task_status**。

---

## 四、上行反馈契约（控制层必须回发的核心）

`/firebot_bridge/command_feedback` 的 JSON 载荷，字段由 `command_receiver.py::on_feedback` 消费：

| 字段 | 必填 | 说明 |
|---|---|---|
| `command_id` | ✅ | 必须与下行命令的 `command_id` 一致 |
| `state` | ✅ | 见下方状态枚举 |
| `task_id` | 否 | 任务类命令回传任务 ID（缺省用命令里的） |
| `reason_code` | 否 | REJECTED/FAILED 时给出原因 |
| `phase` | 否 | EXECUTING 时的阶段（如 `NAVIGATING`） |
| `progress` | 否 | 0–100，EXECUTING/终态时给出 |
| `message` | 否 | FAILED 时的失败描述 |

**state 枚举（`interfaces.py::ROS_FEEDBACK_STATES`）：**
`ACCEPTED` / `EXECUTING` / `COMPLETED` / `REJECTED` / `FAILED` / `CANCELLED`

**状态机（Bridge 如何把 feedback 转成 MQTT）：**

| ROS state | 任务类命令 | 非任务类命令 |
|---|---|---|
| `ACCEPTED` | 回 MQTT `command_ack=accepted` + `task_status=accepted`，保留 pending 等后续 | 回 `command_ack=accepted`，立即终态 |
| `EXECUTING` | 回 `task_status=executing`（带 phase/progress） | 忽略（非任务命令无执行态） |
| `COMPLETED` | 回 `task_status=completed`（progress=100），释放任务锁 | — |
| `FAILED` | 回 `task_status=failed`（reason_code/message），释放任务锁 | — |
| `CANCELLED` | 回 `task_status=cancelled`，释放任务锁 | — |
| `REJECTED` | 回 `command_ack=rejected`（reason_code），释放任务锁 | 回 `command_ack=rejected`，终态 |

**⚠️ 超时约束（最关键）：** `FIREBOT_FEEDBACK_TIMEOUT_SECONDS=3`（默认）。控制层收到命令后**必须在 3 秒内回至少一条 `ACCEPTED`**，否则 Bridge 会回 `rejected / BRIDGE_ADAPTER_NOT_CONNECTED`（当前联调阶段正是这个原因被拒）。

---

## 五、遥测上行契约

| topic | 类型 | 内容 | 转成 MQTT |
|---|---|---|---|
| `/firebot_bridge/battery` | `Float32` | 电量百分比 | `status.battery`（QoS1，1s） |
| `/firebot_bridge/smoke` | `Float32` | 烟雾值 | `sensor.smoke`（QoS0，1s） |
| `/firebot_bridge/status` | `String`(JSON) | `{"mode":..., "estop_active":..., "active_task_id":...}` | `status.mode/estop_active/active_task_id` |
| `/firebot_bridge/location` | `String`(JSON) | 定位信息 | `location`（需 `FIREBOT_LOCATION_ENABLED=true` 且地图身份齐全） |

**电量另一条（已废弃）路径：** Bridge 也订阅 `/robot_status` 读 `battery_percentage` 字段，但当前 `igk_robot/RobotStatus.msg` **没有该字段**，故此路径无效。电量请直接用 `/firebot_bridge/battery`。

**不伪造原则：** 车端不发布 → Bridge 不发（不假报 0/不假报值）。`status` 消息只有「有真实业务字段」才发，`sensor` 只有「有真实 smoke」才发。

---

## 六、关键语义与安全边界

1. **任务锁**：任务类命令（patrol/return_dock/extinguish）同一时刻只允许一个。上一个任务未完成时新任务会被拒 `ACTIVE_TASK_CONFLICT`。
2. **Bridge 不执行运动**：Bridge 只发 `/firebot_bridge/command`，**从不发 `/cmd_vel` 或任何电机话题**。真正驱动机器人（turtle_485 / navigation / 电机）是控制层自己的职责。
3. **急停特殊语义**：`EMERGENCY_STOP` 允许 `target_boot_id` 为空（跨会话急停也能进）；控制层对急停应**无条件、立即**执行，不依赖其他状态。
4. **boot_id 会话**：Bridge 每次进程重启会生成新 boot_id；服务器命令里的 `target_boot_id` 必须匹配当前 boot，否则命令在 Bridge 层就被拒（`ROBOT_BOOT_SESSION_UNKNOWN`）。
5. **capabilities 声明**：`FIREBOT_SUPPORTED_COMMANDS` / `FIREBOT_SENSORS` 声明能力。控制层实际能接哪些命令，就应声明哪些，**不要声明未实现的命令**（声明了又拒绝会破坏上层判断）。

---

## 七、车端控制团队需要交付的东西（工作清单）

### 必须做（闭环控制）
1. **ROS 控制 adapter 节点**：
   - 订阅 `/firebot_bridge/command`（`std_msgs/String`，JSON）。
   - 解析 `command` 字段，映射到实际控制动作（patrol→导航/巡线、stop→停车、estop→急停、return_dock→回充、extinguish→灭火、cancel→取消、manual→手动）。
   - 执行前/执行中/结束后，按第四节状态机回发 `/firebot_bridge/command_feedback`。
   - **收到命令 3 秒内必须先回 `ACCEPTED`**（否则 Bridge 超时判拒）。
   - 检查 `expires_at`，过期命令不执行。

### 必须做（遥测）
2. **电量 provider**：把真实电量（百分比 float）发布到 `/firebot_bridge/battery`。
3. **烟雾 provider**：把真实烟雾值（float）发布到 `/firebot_bridge/smoke`（若有烟雾传感器；没有则留空，不假报）。

### 建议做
4. **status provider**：发布 `/firebot_bridge/status`（mode/estop_active/active_task_id），让平台能看到车端模式与急停状态。
5. **location provider**：发布 `/firebot_bridge/location`（若需上报定位；需先在配置开启 `FIREBOT_LOCATION_ENABLED=true` 并填 SITE/MAP 身份）。

### 可选
6. **alarm**：`/firebot_bridge/alarm` 接口已定义但 Bridge 侧未接线，如需火警告警需与 Bridge 侧协调补上线。

---

## 八、交接验收清单（Checklist）

**下行（Bridge → 控制层）**
- [ ] 控制层能订阅并收到 `/firebot_bridge/command` 的 JSON。
- [ ] 能正确解析 `command` 枚举（8 种命令）。
- [ ] 能区分任务类 / 非任务类命令。
- [ ] 过期命令（`expires_at` 已过）不执行。

**上行反馈（控制层 → Bridge）**
- [ ] 收到命令 3 秒内回 `ACCEPTED`。
- [ ] 任务类命令回 `EXECUTING` → `COMPLETED/FAILED/CANCELLED` 完整序列。
- [ ] `command_id` 原样回传，与下行一致。
- [ ] `REJECTED`/`FAILED` 带 `reason_code`。

**遥测上行**
- [ ] 电量发布到 `/firebot_bridge/battery`，平台 DB 显示真实电量。
- [ ] 烟雾发布到 `/firebot_bridge/smoke`，平台 sensor 显示真实烟雾（若有源）。
- [ ] （可选）status/location 上行正常。

**安全**
- [ ] 急停命令无条件立即执行，且不依赖 boot_id 匹配。
- [ ] 任务锁生效：单任务独占，无并发冲突。
- [ ] 控制层只从 `/firebot_bridge/command` 取命令，不直接听服务器 MQTT（避免绕过 Bridge 校验）。

---

## 九、运行态参考信息

- Bridge 安装目录：`/opt/firebot/vehicle-bridge`
- 配置：`/etc/firebot/bridge.env`（secret 在 `/etc/firebot/bridge-secret.env`，root:root 600）
- 服务：`systemctl status firebot-bridge`；重启 `sudo systemctl restart firebot-bridge`
- Bridge 日志（连接后走 rospy → rosout）：`/home/tl/.ros/log/firebot_bridge_*.log`（journalctl 看不到 MQTT connected）
- 验收脚本：`FIREBOT_BRIDGE_ENV=/etc/firebot/bridge.env bash verify.sh`
- ROS 工作区：`/home/tl/firerobot_ws`（`devel/setup.bash`）
- 源分支/SHA：`ui/youdao-light-hmi-v1` @ `41bbaf4398711fd940dde1818193a67d34e355c8`
