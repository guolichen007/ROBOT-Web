# WEB_V2_1_ACTION_MATRIX — 控制台与编辑器动作矩阵

> 依据基线源码（`23854b0`）逐按钮核对。readiness 列为服务端唯一资格来源；“REAL 本期”一律默认拒绝。合同见 [R002_OPERATION_CONTRACT](../../技术合同/web-v2.1/R002_OPERATION_CONTRACT.md)。

## 1. 监控台动作

| 动作 | 基线 UI 位置 | 基线端点 | 基线资格来源 | 权限 | 确认 | V2.1 readiness action | REAL 本期 | SIM 本期 |
|---|---|---|---|---|---|---|---|---|
| 软件急停 | MonitorView 右栏常驻 + Dock 按住 0.8s | POST `/robots/{vid}/commands/emergency-stop` | `safety_command_ready.emergency_stop` | robot.control.estop | HOLD | emergency_stop | 拒绝（REAL_CONTROL_NOT_AUTHORIZED） | 允许 |
| 急停复位 | Dock | POST `/commands/reset-estop` + 轮询 `/commands/{id}` | `safety_command_ready.reset_estop` | robot.control.reset_estop | HOLD | reset_estop | 拒绝 | 允许 |
| 停止（停止操作） | Dock / 底部状态条 | POST `/robots/{vid}/stop-operation` + 轮询 | `safety_command_ready.stop_motion` + 前端 `useVehicleOperationState` | robot.control.stop（Dock 未做前端判定） | CLICK | stop_motion | 拒绝 | 允许 |
| 保护停车 | **不存在** | — | — | robot.control.stop | CLICK | protective_stop | 拒绝（NOT_IMPLEMENTED） | 合同演练 |
| 开始巡检 | Dock | GET `/patrol-plans` → POST `/tasks/patrol-plan` | `autonomous_task_ready.patrol` + 前端等待区硬编码 (1.2,1.2) | patrol.create（未前端判定） | DOUBLE | patrol_start | 拒绝 | 允许（仅已加载场景） |
| 暂停/恢复巡检 | VehicleAgentControls（仅 V1） | POST `/robots/{vid}/agent/commands/stop_patrol｜resume_patrol` | `vehicle_agent_ready[...]` | 动态映射 | CLICK | patrol_pause / patrol_resume | 拒绝 | 合同演练 |
| 继续巡检（Canonical） | Dock（resume_task_id） | POST `/tasks/patrol-plan` | `resume_state=AVAILABLE` | patrol.create | CLICK | patrol_resume | 拒绝 | 允许 |
| 目标导航 | MapSelectionBar / V1 navigate | POST `/robots/{vid}/navigate-preset` 或 agent navigate | 前端 `navigationReason` + readiness | patrol.create | CLICK | navigate_to_point | 拒绝 | 允许 |
| 返航 | Dock | POST `/tasks/return-dock` | `autonomous_task_ready.return_dock` | patrol.create | CLICK | return_home | 拒绝 | 允许 |
| 取消任务 | 任务页 | POST `/tasks/{id}/cancel` | 无前端资格 | robot.control.task | CLICK | cancel_mission | 拒绝 | 允许 |
| 手动控制 | `ManualControl.vue`（**未挂载**） | POST `/manual-lease`、`/commands/manual`（150ms）、`stop-motion` | `manual_control_ready` | robot.control.manual | HOLD 方向键 | manual_control | 拒绝 | 允许 |
| 启动/关闭车辆 | VehicleAgentControls / Diagnostics（V1） | agent `start｜close` | `vehicle_agent_ready` | 动态映射 | DOUBLE | start_vehicle / shutdown_vehicle | 拒绝 | 合同演练 |
| 防火罩 开/关/停 | **不存在** | — | — | robot.control.device（新） | HOLD | fire_shield_* | 拒绝 | 合同演练 |
| 告警确认/确认火情/解决 | PrimaryAlarmPanel、AlarmCenter | POST `/alarms/{id}/acknowledge｜confirm｜resolve` | 权限 | alarm.ack/confirm/resolve | CLICK | （非运动） | 允许（非下行） | 允许 |
| 灭火任务 | ExtinguishActionCards | POST `/alarms/{id}/create-task` | `autonomous_task_ready.extinguish` | extinguish.create | DOUBLE | （暂缓，非本期） | 拒绝 | 不做 |
| 人工上报火情 | MonitorView | POST `/alarms/manual` | 权限 | alarm.* | CLICK | （非运动） | 允许 | 允许 |
| 视频连接 | VideoSurveillancePanel | `/media/tickets` + WHEP | 媒体票据 | robot.read | CLICK | — | 允许（只读） | 允许 |

## 2. 地图场景编辑器动作

| 动作 | 基线 | V2.1 | 权限 | 服务端约束 |
|---|---|---|---|---|
| 导入 PGM/PNG+YAML | 无（只有 `/assets` 图片上传） | 前端预检 + 服务端复检 | map.edit | 尺寸/像素/路径/有限数（SCENE §4） |
| 新建草稿（复制 PUBLISHED） | POST `/map-versions`（不复制子资源） | 复制为新 DRAFT | map.edit | 只从 PUBLISHED 复制 |
| 点位增删改/拖动/旋转/精确输入 | 巡检/灭火点仅 POST/PUT，无 DELETE | 统一点位 CRUD | map.edit | 仅 DRAFT；**禁止跨版本（已修）**；删除被引用点位 409 |
| 当前位置示教 | 无 | 用 VehicleSnapshot.pose（LIVE 且 MAP 一致） | map.edit | pose 质量非 LIVE 时拒绝 |
| 车位区域/编号 | POST/PUT/DELETE `/parking-slots` | 同左 + 多边形编辑 | map.edit | 编号版本内唯一 |
| 路线编辑/排序/方向预览 | `/trajectories` 坐标列表 | 点位引用 + 方向箭头 | map.edit | 引用同版本 |
| 右车道走廊 | 无（route_builder 写死） | 折线 + 宽度 | map.edit | 方向=折线顺序 |
| 撤销/重做 | 无 | 编辑器本地命令栈 | — | 不落库 |
| 保存草稿 | 各资源独立 PUT | 带 revision 条件保存 | map.edit | 409 REVISION_CONFLICT |
| 发布 | POST `/map-versions/{id}/publish`（不算摘要、不归档旧版） | 校验 + digest + 发布 | map.publish | 发布≠加载≠运动授权 |
| 推送车端加载 | 无 | SIM 演练 | map.publish | 回执匹配才 LOADED_CONFIRMED |
| 归档 | POST `/archive`（无审计） | 补审计 | map.publish | 不清 active_version_id 的问题记入差距 |
