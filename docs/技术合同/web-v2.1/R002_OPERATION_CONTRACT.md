# R002_OPERATION_CONTRACT — 控制资格、任务、设备、告警与诊断合同

> CONTRACT_VERSION=`2.1.0-draft.1` · 状态：**DRAFT，未冻结** · 基线 `23854b0`
> 机器可读：[firebot-web-v2.1.contracts.schema.json](../../../packages/protocol-schemas/firebot-v2.1/firebot-web-v2.1.contracts.schema.json)（VehicleSnapshot / OperationReadiness / MissionRequest / MissionFeedback / DeviceOperation / AlarmEvent / NavigationDiagnostics）；向量 [vectors/](../../../packages/protocol-schemas/firebot-v2.1/vectors/)；规则 [contracts_v21.py](../../../services/contracts_v21.py)；场景见 [R002_SCENE_CONTRACT.md](R002_SCENE_CONTRACT.md)。
> 上位：[车辆安全责任合同.md](../车辆安全责任合同.md)（最终运动安全由车端负责）、[MQTT协议.md](../MQTT协议.md)、[FIREBOT_BRIDGE_CONTRACT_1.3.md](../FIREBOT_BRIDGE_CONTRACT_1.3.md)。

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

复用 `fire_events`（状态 NEW→ACK/CONFIRMED/DISMISSED→RESOLVED→CLOSED，`apps/api/app/modules/alarms/router.py:334`）与审计。新增类别：SMOKE_HIGH、TEMPERATURE_HIGH、BATTERY_LOW、DEVICE_FAULT、LINK_LOSS、NAVIGATION_FAULT、ESTOP。阈值：电量 <50%（percent）、温度 ≥80℃（celsius，当前无数据源）、烟雾 ≥300ppm **仅当 unit_verified=true**；未验证时 `unit=UNVERIFIED, threshold=null`，平台规则不得生成 SMOKE_HIGH。状态非 NEW 必须有 handling 记录（确认人/时间）。火情弹窗携带 position/parking_slot_code 供地图定位。

## 6. NavigationDiagnostics

映射 ingress `external_goal_id`（`services/mqtt-ingress/main.py:604`），补 planner_state、localization、obstacle、distance_to_goal、recovery_count。`planner_state=BLOCKED` 必须 `obstacle.blocked=true`。

## 7. Dispatcher 最终发布门禁（P0，待审查后实施）

**现象**：三条下发路径——Outbox（`dispatch_next`）、Redis 人工控制 pubsub（`manual_loop`，QoS0）、Redis 安全命令 Stream（`process_safety_events`）——都汇入 `services/command-dispatcher/main.py:publish_command()`。VEHICLE_AGENT_V1 分支有全局开关、逐车 allowlist、boot 比对；**CANONICAL_MQTT 分支只做 schema 与过期检查**，无逐车 allowlist、无 `target_boot_id==robot.boot_id`、无环境隔离、无控制冻结。门禁只在 API 层（readiness），绕过 API 写入 Outbox/Redis 即可下发。

**根因**：最终发布点没有独立于 API 的 fail-closed 校验。

**精确修法（单点，覆盖三路径）**：在 `publish_command()` 解析 `robot` 之后、`target.publish` 之前，对所有协议执行 `final_publish_gate(payload, robot, settings)`：
```
deny unless settings.canonical_downlink_allowed(robot.vehicle_id)      # 新逐车白名单，默认空
deny if payload.cmd != "emergency_stop" and payload.target_boot_id != robot.boot_id
deny if is_sim_namespace(robot.vehicle_id) != (deployment_env == "SIM")  # SIM/REAL 隔离
deny if robot.control_frozen                                            # 冻结标志
on deny: raise GateDenied(code) -> Outbox: FAILED 终态(不重试)；manual: 丢弃+计数；safety: 置 REJECTED 并 xack
```
测试仅反证根因：对三条路径各构造一条绕过 API 的 payload，断言 0 次 `client.publish`。
**DO_NOT_TOUCH**：V1 分支既有校验顺序；001 的 `transport_profile`；Outbox 排序与退避。
**兼容影响**：001 若走 Canonical 下行，部署前必须把 001 显式加入新白名单，否则 001 下行被拒——这正是需要独立审查与 OWNER 决定的原因。emergency_stop 是否也受白名单约束，需 OWNER 决定（建议：受白名单约束，但 boot 不必匹配）。

## 8. 兼容性汇总

| 变更 | 类型 | 影响 | 状态 |
|---|---|---|---|
| 9 个平台合同 schema | 新增，平台内 | 无线缆影响 | DRAFT |
| `robot/{vid}/scene`、`scene_receipt` | MQTT 新 topic | 车端需实现 | 未冻结 |
| task_status 扩展（goal_id、WAITING_OBSTACLE、PAUSED、final_pose） | 1.3→1.4 | 车端/ingress | 未冻结 |
| patrol params 增加 scene_*、map_checksum | 1.3 params 内容 | 车端校验 | 未冻结 |
| `robot.control.device` 权限 | RBAC | seed/角色 | 未冻结 |
| dispatcher 最终门禁 | 控制路径 | 001 需白名单 | 待审查 |
| 地图子资源跨版本修复 | API 行为收紧 | 旧客户端若依赖迁移将 409 | **已实施** |
