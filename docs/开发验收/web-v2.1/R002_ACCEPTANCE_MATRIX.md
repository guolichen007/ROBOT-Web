# R002_ACCEPTANCE_MATRIX — Web V2.1 验收矩阵

> 状态取值：**PASS**（本轮已执行且通过，附命令/证据）· **SPEC**（合同/规则已定义并有机器可读向量，产品路径未实现）· **NEXT-STAGE**（下一阶段实现）· **BLOCKED**（需车端/VM/OWNER 输入）。
> 证据目录：[evidence/](evidence/)；合同向量：[vectors/](../../../packages/protocol-schemas/firebot-v2.1/vectors/)。所有 PASS 均在隔离 SIM 栈执行，`VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false`。

## 1. 任务书“重要反例”

| # | 反例 | 状态 | 证据 / 测试 |
|---|---|---|---|
| N1 | 已发布地图禁止更新 | PASS（API） | `apps/api/tests/test_web_v21_map_isolation.py::test_published_slot_content_still_frozen`；编辑器 `SCENE_NOT_DRAFT`（`tests/scene-editor.test.ts`） |
| N2 | 草稿点位禁止跨版本迁移 | PASS（API，已修复） | 同文件 `test_slot_cannot_move_to_another_draft_version`、`test_slot_cannot_migrate_into_published_version`、`test_inspection_point_cannot_move_across_versions`、`test_extinguish_point_and_trajectory_cannot_move_across_versions`；修复前 5 项 FAIL（红灯先行） |
| N3 | 并发编辑不静默覆盖 | SPEC | `base_scene_version`/revision 已入合同；DB 迁移待审查（SCENE §6）。现有计划模块已有条件更新先例 |
| N4 | 无效地图坐标及姿态拒绝 | PASS（合同/前端） | 向量 `VehicleSnapshot.nan_yaw_literal`、`SceneManifest.point_coord_absurd` 等；`tests/nav-map.test.ts`（.nan/.inf/越界）；浏览器 `NEG_NON_FINITE_INPUT` |
| N5 | 删除被引用点位拒绝 | PASS（API 车位、前端点位、合同） | `test_delete_slot_referenced_by_point_refused`（原先级联静默删除）；编辑器 `POINT_REFERENCED_BY_ROUTE`（单测 + 浏览器 `NEG_DELETE_REFERENCED_POINT`）；`contracts_v21.can_delete_point` |
| N6 | 未加载场景禁止创建真实任务 | SPEC | `test_contracts_v21.py::test_unloaded_scene_cannot_create_mission_and_publish_is_not_motion_authorization`；接入 `/tasks` 需 DB 迁移 |
| N7 | 旧 boot_id、Goal、过期反馈不更新当前任务 | SPEC | `MissionFeedback.stale_cases`（old_boot/old_goal/replayed_seq/after_terminal）；现 ingress `handle_task_status` 不满足（差距 P1） |
| N8 | 无权限或控制冻结时 API 与最终发布路径拒绝 | SPEC（API 部分现有） | 合同 `FROZEN_BUT_ACTION_ALLOWED`；dispatcher 最终门禁规格 OPERATION §7，**待审查** |
| N9 | 不产生真实 MQTT 下行 | PASS | 地图测试 autouse `zero_downlink`（Command/Outbox 行数前后不变）；浏览器流 `ZERO_SERVER_WRITES`；栈配置下行开关 false |

## 2. 功能验收

| 需求 | 验收标准 | 状态 | 证据 |
|---|---|---|---|
| RQ-10 导入 | 合法 YAML+PGM 导入并显示元数据；路径穿越/NaN/超尺寸/重复键/别名/未知键拒绝 | PASS | `tests/nav-map.test.ts`（25 项）；浏览器 `IMPORT_META`、`NEG_YAML_TRAVERSAL` |
| RQ-11 坐标 | 左下角=origin、第 0 行=顶；带旋转原点往返误差 <1e-9；MapAdapter 屏幕往返 | PASS | `tests/nav-map.test.ts` NavMapGrid 组 |
| RQ-12 点位 | 新增/拖动（一次撤销一步）/旋转（归一化）/精确输入/当前位置示教（仅 LIVE+OK+同地图） | PASS（本地草稿） | 单测 + 浏览器 `ADD_3_POINTS`、`PRECISE_INPUT`、`DRAG_UNDO_REDO_ONE_STEP` |
| RQ-13 路线/走廊 | 排序、反向、方向预览；右车道在行进方向右侧 | PASS（模型+路线 UI） | `tests/scene-editor.test.ts`；车位/走廊 UI 为 NEXT-STAGE |
| RQ-14 撤销/校验 | 撤销/重做；可通行性永不自动通过 | PASS | 浏览器 `VALIDATE_PENDING_TRAVERSABILITY` |
| RQ-15 发布/加载 | 回执 boot/digest/版本/导航图校验和全部匹配才 LOADED_CONFIRMED | SPEC | `test_scene_load_confirmation_requires_exact_match` |
| RQ-01..09 控制台 | 两档分辨率关键操作不出折叠线；保护停车常驻；资格只来自 readiness | NEXT-STAGE | 基线问题已截图：`evidence/web0-baseline/baseline_monitor_1366x768.png` |
| RQ-19 阈值 | 烟雾单位未验证不得显示 ppm 或套 300 | SPEC | 向量 `smoke_ppm_unverified`、`fake_ppm`、`unverified_with_threshold`；现前端显示“%”（P1） |
| RQ-21 PREVIEW_ONLY | 保护不被删除或绕过 | PASS（未改动） | 本轮未触碰 plans/worker/约束 |
| RQ-22 车云 SIM | 真实 API/PG/Broker/WS 闭环 | BLOCKED | 等 VM 按本合同实现 SIM 执行适配 |

## 3. 两档分辨率截图

| 页面 | 1366×768 | 1920×1080 |
|---|---|---|
| 基线监控台 | [baseline_monitor_1366x768.png](evidence/web0-baseline/baseline_monitor_1366x768.png) | [baseline_monitor_1920x1080.png](evidence/web0-baseline/baseline_monitor_1920x1080.png) |
| 基线地图版本 | [baseline_maps_1366x768.png](evidence/web0-baseline/baseline_maps_1366x768.png) | [baseline_maps_1920x1080.png](evidence/web0-baseline/baseline_maps_1920x1080.png) |
| 场景编辑器（新） | [web2_editor_1366x768.png](evidence/web2-editor/web2_editor_1366x768.png) | [web2_editor_1920x1080.png](evidence/web2-editor/web2_editor_1920x1080.png) |

其余基线页（parking/patrol/tasks/alarms）见 [evidence/web0-baseline/](evidence/web0-baseline/)。
