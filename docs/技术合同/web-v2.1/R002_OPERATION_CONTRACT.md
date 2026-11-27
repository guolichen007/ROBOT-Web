# R002_OPERATION_CONTRACT — 控制资格、任务、设备、告警与诊断合同

> CONTRACT_VERSION=`2.1.0-draft.3` · 状态：**DRAFT，未冻结** · 基线 `23854b0`
> 机器可读：[firebot-web-v2.1.contracts.schema.json](../../../packages/protocol-schemas/firebot-v2.1/firebot-web-v2.1.contracts.schema.json)（VehicleSnapshot / OperationReadiness / MissionRequest / MissionFeedback / DeviceOperation / AlarmEvent / NavigationDiagnostics）；向量 [vectors/](../../../packages/protocol-schemas/firebot-v2.1/vectors/)；规则 [contracts_v21.py](../../../services/contracts_v21.py)；场景见 [R002_SCENE_CONTRACT.md](R002_SCENE_CONTRACT.md)。
> 上位：[车辆安全责任合同.md](../车辆安全责任合同.md)（最终运动安全由车端负责）、[MQTT协议.md](../MQTT协议.md)、[FIREBOT_BRIDGE_CONTRACT_1.3.md](../FIREBOT_BRIDGE_CONTRACT_1.3.md)。

## draft.3 变更（R1B，2026-11-27）——独立审查 6 类错误接受

| # | 错误接受（draft.2） | draft.3 规则 | 拒绝码 |
|---|---|---|---|
| 1 | waypoint_index 0→2 跳跃 | 航点只能逐个推进；PASS_THROUGH 点也须按序上报（到达但不要求停车） | `WAYPOINT_SKIPPED` |
| 2 | 新航点复用旧 Goal ID | 任务记录 `goals_used`（当前绑定 goal 恒计入）；新航点必须新 goal | `GOAL_REUSED` |
| 3 | 中间航点报告 SUCCEEDED | SUCCEEDED 只允许在最后一个航点 | `INTERMEDIATE_SUCCEEDED` |
| 4 | snapshot map_checksum 与 manifest 不一致 | LIVE 位姿必须带 map_checksum，准入与成功确认均比对 | `NAV_MAP_MISMATCH` |
| 5 | 未来约 10 分钟的 snapshot | snapshot `generated_at` 超前 >2 s 拒绝 | `SNAPSHOT_FROM_FUTURE` |
| 6 | OFFLINE / STALE 位姿 + 旧 readiness 通过准入 | 准入要求链路 ONLINE、位姿 LIVE 且定位 OK，并要求 readiness 由**本** snapshot 计算 | `LINK_NOT_ONLINE` / `POSE_NOT_LIVE` / `READINESS_NOT_FROM_THIS_SNAPSHOT` |

**经过点与必须到达点**：航点 `arrival = PASS_THROUGH | STOP`；巡检路线至少一个 STOP，最后航点为成功锚点。不要求中间点停车。

**任务最终成功**（`confirm_mission_success`）：车端 SUCCEEDED 只是声明。平台确认需同时满足：最后航点为 STOP；平台自有快照位姿 LIVE、定位 OK、与请求 `nav_map_checksum` 同图；位姿观测晚于 SUCCEEDED 声明且 ≤2 s；实测静止（`motion.stationary=true`、LIVE）；位置与朝向在该航点容差内（默认 0.2 m / 0.15 rad）。

**readiness 权威来源与前置条件**：`source = SERVER_OPERATION_READINESS` 才能准入运动；`LEGACY_ROBOT_READINESS`（旧 `robot_readiness()` 兼容投影）只用于展示，Schema 语义规则禁止它允许任何运动类动作。`based_on_snapshot_at` 必须等于准入时使用的 snapshot `generated_at`。

**冻结语义**：`freeze_scope = ALL | ALL_EXCEPT_STOP_MOTION`。**REAL 冻结恒为 ALL，包含未经授权的软件急停**（远程软件急停也是远程命令；现场急停为物理装置）。SIM 可用 `ALL_EXCEPT_STOP_MOTION` 保留 stop_motion / protective_stop。001 维持 V1 独立冻结，不再等待 Canonical 白名单决定。

**场景回执**：`apply_scene_receipt` 只接受**当前最新** load_id、当前 boot、与 manifest 不可变身份（id+version+digest）一致的回执；旧回执与重复回执不覆盖已有结果。

## draft.2 变更（HF1，2026-11-27）

- **反馈准入改为纯 reducer** `apply_feedback(task, feedback, now)`，校验顺序：车辆→环境→任务→`command_id`→boot→`execution_gen`（旧代次/未知代次）→终态→新鲜度（>10 s 过期、超前 >2 s）→seq（含精确重放）→航点总数/航点不回退→goal 绑定（同航点已绑定 goal 时空 goal=`GOAL_REQUIRED`，修复 CE1）→合法迁移表（`EXECUTING→ACCEPTED` 等=`ILLEGAL_TRANSITION`，修复 CE2）。
- **准入带时间**：`mission_admissible(..., now)` 拒绝过期 readiness（`now >= valid_until`，修复 CE4）、未来 readiness、陈旧快照（>5 s）、冻结、boot 未知。
- MissionRequest/MissionFeedback 新增必填 `command_id`、`execution_gen`（续巡 +1，`resume_of` 时 gen 必须 ≥2）；Feedback `waypoint_index/waypoint_total` 必填；`EXECUTING/WAITING_OBSTACLE/SUCCEEDED` 必带 `goal_id`。
- **阈值精确边界**（`threshold_alarms`，仅 LIVE 触发）：电量 `< 50`、温度 `> 80`、烟雾 `> 300` 且 `unit=ppm, unit_verified=true`；恰好 50/80/300 不报警。
- **分层**：`x-tier=core` 的合同可独立联调；`DeviceOperation`、`AlarmEvent` 为 extension；`VehicleSnapshot` 的烟雾/温度/设备改为可选，不阻塞导航联调。
- 证据：5 项反例修复前 ACCEPTED_WRONGLY、修复后全部拒绝；11 个变异体全部被测试杀死（见证据包）。

## 0. 不变式

1. **REAL 默认拒绝。** REAL 车辆 `OperationReadiness.control_frozen=true`，`freeze_reasons` 含 `REAL_CONTROL_NOT_AUTHORIZED`，直到 OWNER 专项授权；样例 `OperationReadiness.real_default_deny`。
2. **前端不推导资格。** 按钮可用性只来自 `actions[action].allowed`；缺失的 action 视为拒绝。
3. **地图 CRUD/草稿/发布/加载确认 ≠ 运动授权**（`mission_admissible` 同时要求场景已加载与 readiness 允许）。
4. **SIM/REAL 硬隔离。** SIM 车辆仅 `firebot-sim-*` 命名空间；REAL 永不在该命名空间（schema `envBinding`）。SIM 的库、凭证、Broker topic 与 REAL 分离。
5. **未知≠0。** quality 非 LIVE/STALE 时 value 必须为 null。
6. **不产生真实 MQTT 下行。** 本轮所有验证在隔离栈进行，`VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false`。

## 1. VehicleSnapshot（服务器→Web）

字段与现有实现映射：

| 合同 | 现有来源 | 备注 |
|---|---|---|
| link.state | `robots.online_state`（task-worker 3s STALE / 10s OFFLINE） | UNKNOWN=从未上线 |
| pose.value.x/y/yaw | Redis `robot:{vid}:latest` 的 `x/y/theta` | **theta→yaw**；`map_contract_verified=false` 时 ingress 只给 `diagnostic_pose` → quality=UNKNOWN，localization=MAP_UNVERIFIED |
| pose.observed_at | `source_timestamp`；接收时刻 `server_received_at` | 两个时间都要显示 |
| pose.map_checksum | 1.3 location.map_checksum | ingress **未持久化**（P1） |
| battery_pct | 1.3 location/status.battery；v1 `telemetry.battery.percentage` | |
| smoke | 1.3 sensor.smoke；v1 `telemetry.smog.percentage` | 单位未定义 → `UNVERIFIED`；现前端写 `%`（P1，需改） |
| temperature_c | G3-R2 已禁用 | quality=UNSUPPORTED |
| estop | `robots.estop_active` / status.estop_active | ROS1 只读源无急停话题 → UNKNOWN |
| devices[] | `robot_data_channels` + v1 programs | 防火罩无来源 → UNKNOWN |
| scene.* | 无（仅 `robots.current_map_version`） | 新增 |

## 2. OperationReadiness（服务器→Web）

由现有 `robot_readiness()`（`apps/api/app/modules/commands/readiness.py:41`）演进，不另起炉灶：

| 合同 action | 现有键 | 权限 |
|---|---|---|
| stop_motion | `safety_command_ready.stop_motion` | robot.control.stop |
| emergency_stop / reset_estop | `safety_command_ready.*` | robot.control.estop / reset_estop |
| protective_stop | 现有 stop-operation 流程（`/robots/{vid}/stop-operation`） | robot.control.stop |
| patrol_start / return_home | `autonomous_task_ready.patrol / return_dock` | patrol.create |
| navigate_to_point | V1 `vehicle_agent_ready.navigate`；Canonical 走 `patrol`+mission_kind | patrol.create |
| patrol_pause / patrol_resume | V1 `stop_patrol/resume_patrol`；Canonical 无 | patrol.create |
| cancel_mission | `/tasks/{id}/cancel` | robot.control.task |
| manual_control | `manual_control_ready` + 租约 | robot.control.manual |
| start_vehicle / shutdown_vehicle | V1 `start/close` | 按 vehicle_agent_router 映射 |
| fire_shield_* | **无** | 新增权限 `robot.control.device`（需审查） |
| scene_load | 无 | map.publish |

`reasons` 复用 `readiness_reasons` 代码集（ROBOT_OFFLINE、CONTROL_CONTRACT_NOT_VERIFIED 等），新增：REAL_CONTROL_NOT_AUTHORIZED、SCENE_NOT_LOADED_ON_VEHICLE、CONTROL_LEASE_HELD_BY_OTHER、DEVICE_FEEDBACK_CONTRACT_NOT_VERIFIED、PROTECTIVE_STOP_NOT_IMPLEMENTED。`permitted_for_user` 由服务端按当前用户权限计算（当前 Dock 内巡检/停止/返航按钮缺前端权限判定，见差距报告）。`control_owner` 来自手动控制租约（`/robots/{vid}/manual-lease`）。`boot_id=null` 时除 emergency_stop 外一律不可用（与 1.3 `target_boot_id` 规则一致）。

## 3. MissionRequest / MissionFeedback

**线缆映射（1.3 不变）**：MissionRequest → `robot/{vid}/command`，`cmd=patrol`（NAVIGATE/RETURN 用 `params.mission_kind`，现有 `operations/router.py:272` 已如此），`command_id`=mission 下发命令 id，`task_id`=mission_id，`target_boot_id` 必填。`params` 需补 `scene_id/scene_version/scene_digest/map_checksum`（现缺 map_checksum，P1）。MissionFeedback ← 1.3 `task_status`（status/phase/progress/waypoint_*），需扩展 `goal_id`、`WAITING_OBSTACLE/PAUSED` 状态、`obstacle_since`、`final_pose`。

**幂等**：沿用 `Idempotency-Key`（`apps/api/app/core/idempotency.py`），mission_id 唯一。

**路线快照**：任务创建时冻结 `route_snapshot.waypoints`（来自已加载的 PUBLISHED 场景），之后场景再变不影响在途任务。**禁止 route_builder 的 DEMO_PARKING 几何进入 R002 任务。**

**反馈准入**（`feedback_applies`）：vehicle/mission 匹配、boot_id = 任务下发时 boot、goal_id 一致、seq 单调、非终态；否则丢弃并记诊断。现 ingress `handle_task_status` 不校验 boot_id、不阻止终态回退（P1）。

**终态确认**：SUCCEEDED 需 `final_pose`，服务端沿用 VERIFYING_FINAL_POSE 连续 3 帧核验（task-worker:390）；“真实停车确认”=服务端核验通过，而非车端自报。

**预留状态**（本期合同已定义，车端实现待接）：WAITING_OBSTACLE（障碍等待）、PAUSED（人工暂停）、安全保护（SAFETY 来源的 cmd_vel，`NavigationDiagnostics.cmd_vel_source`）、设备故障（`devices[].state=FAULT`+`fault_code`）、机械状态（`DeviceOperation.result.position/limit_*`）、控制权（`control_owner`）。

## 4. DeviceOperation（防火罩人工操作）

只允许 `control_mode=MANUAL`（自动作业暂缓）。COMPLETED 必须有限位开关确认（OPEN→limit_open=true 且 limit_closed=false）。无限位反馈时最多显示 IN_PROGRESS/UNKNOWN，**不得宣称完成**。REQUEST 需 target_boot_id、有效期；ACK/RESULT 带 boot_id+seq。现仓库无防火罩驱动/端点（`PatrolView` 文案仅提示“需授权”）；本期 Web 只做面板与合同，`fire_shield_*` 在 REAL 恒为 `DEVICE_FEEDBACK_CONTRACT_NOT_VERIFIED`。

## 5. AlarmEvent

复用 `fire_events`（状态 NEW→ACK/CONFIRMED/DISMISSED→RESOLVED→CLOSED，`apps/api/app/modules/alarms/router.py:334`）与审计。新增类别：SMOKE_HIGH、TEMPERATURE_HIGH、BATTERY_LOW、DEVICE_FAULT、LINK_LOSS、NAVIGATION_FAULT、ESTOP。阈值（精确边界）：电量 <50%（percent）、温度 >80℃（celsius，当前无数据源）、烟雾 >300ppm **仅当 unit_verified=true**；恰为 50/80/300 不报警；未验证时 `unit=UNVERIFIED, threshold=null`，平台规则不得生成 SMOKE_HIGH。状态非 NEW 必须有 handling 记录（确认人/时间）。火情弹窗携带 position/parking_slot_code 供地图定位。

## 6. NavigationDiagnostics

映射 ingress `external_goal_id`（`services/mqtt-ingress/main.py:604`），补 planner_state、localization、obstacle、distance_to_goal、recovery_count。`planner_state=BLOCKED` 必须 `obstacle.blocked=true`。

## 7. Dispatcher 最终发布门禁（WEB-R1A 已在候选分支实施，待独立审查）

**单点**：`services/dispatch_gate.py:final_publish_gate()`，由 `services/command-dispatcher/main.py:publish_command(payload, qos, path)` 在 CANONICAL_MQTT 分支、`client.publish` 之前调用。三入口均经过：Outbox（`dispatch_next`，path=outbox）、Redis 人工控制 pubsub（`manual_loop`，path=manual）、Redis 安全 Stream（`process_safety_events`，path=safety）。

**范围**：仅 CANONICAL_MQTT。VEHICLE_AGENT_V1 分支（001 独立接入）与 ROS_COMPAT 拒绝逻辑**未改动**；001 不加入 Canonical 白名单。

**配置**（`apps/api/app/core/config.py`）：`FIREBOT_DEPLOYMENT_ENV=REAL|SIM`（默认 REAL）、`CANONICAL_DOWNLINK_ALLOWLIST`（默认空 = REAL 默认拒绝）、`DOWNLINK_FROZEN`（默认 false）。仅 `compose.test.yml` 测试栈放行 mock 车 `R001,R002`。

| 顺序 | 拒绝码 | 条件 |
|---|---|---|
| 1 | FG_DOWNLINK_FROZEN | `DOWNLINK_FROZEN=true` |
| 2 | FG_ENV_INVALID / FG_ENV_MISMATCH | 环境非 REAL/SIM；SIM 环境只放 `firebot-sim-*`，REAL 环境拒绝 `firebot-sim-*` |
| 3 | FG_ENV_CREDENTIALS | SIM 环境却连接生产 Broker（`firebot.youdaozd.com`） |
| 4 | FG_NOT_ALLOWLISTED | 车辆不在 Canonical 白名单 |
| 5 | FG_IDENTITY_MISMATCH | payload 车辆与解析车辆不一致 |
| 6 | FG_BOOT_MISMATCH | `target_boot_id != robot.boot_id`（仅 emergency_stop 可为 null） |
| 7 | FG_EXPIRED / FG_TTL_INVALID | 已过期；签发超前 >2 s、TTL >240 s 或 expires≤issued（过期在门禁前已由原逻辑转 EXPIRED） |
| 8 | FG_UNAUTHORIZED | 非人工命令：须有同车、同 cmd、**payload 逐字节相等**、生命周期为 QUEUED(outbox)/CREATED(safety) 的 Command 行；人工控制：须走 manual 路径且 Redis 租约 lease_id/control_session_id/user 全匹配；路径与命令类型交叉注入一律拒绝 |
| 9 | FG_REPLAY | `dispatch:fg:claim:{command_id}` 已存在（SET NX）；Broker 发布失败会释放 claim，重试不算重放 |

**拒绝处理**：Outbox → `REJECTED`（不重试）+ Command `GATE_REJECTED`/`ack_reason=码`；安全 Stream → Command `GATE_REJECTED` 并 xack；人工 → 丢帧。每次拒绝计数 `dispatch:fg:denied:{path}:{code}` 并记日志。

**证据**：`apps/api/tests/test_web_r1a_final_publish_gate.py` 27 项（每项断言捕获客户端 0 次 publish）；11 个门禁变异体全部被杀死。

**部署影响（需 OWNER 知悉）**：部署后在未配置白名单前，所有 Canonical 下行（含 R002 停止/急停/任务）均被门禁拒绝，界面显示 `GATE_REJECTED`；这正是当前 REAL 控制 NO-GO 的预期。原 `test_legacy_canonical_transport_and_v1_mutual_exclusion` 中“无授权 Command 直发 R002 成功”的断言已改为断言被拒（有意的语义变更）。

## 8. 兼容性汇总

| 变更 | 类型 | 影响 | 状态 |
|---|---|---|---|
| 9 个平台合同 schema | 新增，平台内 | 无线缆影响 | DRAFT |
| `robot/{vid}/scene`、`scene_receipt` | MQTT 新 topic | 车端需实现 | 未冻结 |
| task_status 扩展（goal_id、WAITING_OBSTACLE、PAUSED、final_pose） | 1.3→1.4 | 车端/ingress | 未冻结 |
| patrol params 增加 scene_*、map_checksum | 1.3 params 内容 | 车端校验 | 未冻结 |
| `robot.control.device` 权限 | RBAC | seed/角色 | 未冻结 |
| dispatcher 最终门禁 | 控制路径 | 001 走 V1 不受影响；Canonical 默认全拒 | **已实施（候选分支），待审查** |
| 地图子资源跨版本修复 | API 行为收紧 | 旧客户端若依赖迁移将 409 | **已实施** |
