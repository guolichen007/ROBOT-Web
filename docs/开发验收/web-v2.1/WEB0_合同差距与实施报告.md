# WEB-V2.1 WEB-0 合同差距与实施报告（供独立审查）

> 基线 `23854b02cd32f4e36a88826ad2293d9c623c8f38`（= origin/feature/web-r1-min，2026-11-26 远端核实）· 分支 `feature/firebot-r002-web-v2-1`（本地，未推送）· CONTRACT_VERSION `2.1.0-draft.1`（未冻结）
> 结论：**WAIT** —— WEB-0 合同与首批 WEB-2 成果可审查；P0（dispatcher 最终门禁）与 DB 迁移需独立审查后方可实施。

```
REAL_CONTROL=NONE (未新增、未放开任何真实控制路径)
PRODUCTION_MUTATION=NONE
REAL_VEHICLE_COMMANDS=0
DO_NOT_TOUCH(本轮零改动)：command-dispatcher / mqtt-ingress / task-worker / plans 与 PREVIEW_ONLY 约束 / readiness / vehicle-agent / 1.3 schema / Alembic
```

## 1. 本轮实际变更（行为变更仅 1 处后端）

| 类别 | 文件 | 说明 |
|---|---|---|
| 后端修复 | `apps/api/app/modules/maps/router.py` | 子资源跨版本迁移禁止；点位车位同版本；删除被引用车位拒绝 |
| 后端测试 | `apps/api/tests/test_web_v21_map_isolation.py` | 7 项反证 + 零下行守卫 |
| 合同 | `packages/protocol-schemas/firebot-v2.1/*`、`services/contracts_v21.py` | 9 合同 schema、生成器、向量、语义规则 |
| 合同测试 | `services/protocol-tester/tests/test_contracts_v21.py` | 107 项 |
| 前端库 | `apps/web/src/lib/nav-map.ts`、`scene-editor.ts` | 导入/坐标/编辑模型 |
| 前端页 | `apps/web/src/views/SceneEditorView.vue`、`router.ts`、`App.vue` | `/maps/editor`（map.edit） |
| 前端测试/脚本 | `apps/web/tests/nav-map.test.ts`、`scene-editor.test.ts`、`scripts/v21-*.mjs` | 37 单测；浏览器流 18 组断言；截图脚本（仅 loopback） |
| 文档 | `DESIGN.md`、`docs/技术合同/web-v2.1/*`、`docs/开发验收/web-v2.1/*` | 6 份 + 本报告 + 证据 |

## 2. 差距清单（只列新问题；按严重度）

### P0-1 dispatcher 最终发布门禁缺失（Canonical 路径）
- 现象：`services/command-dispatcher/main.py:publish_command()` 的 CANONICAL_MQTT 分支只做 schema + 过期检查；无逐车白名单、无 `target_boot_id==robot.boot_id`、无环境隔离、无冻结标志。Outbox、Redis 人工控制、Redis 安全 Stream 三路径都汇入此函数。
- 根因：控制资格只在 API（readiness）计算，最终发布点不独立复核；绕过 API 写入 Outbox/Redis 即可下发。
- 精确修法：见 [R002_OPERATION_CONTRACT §7](../../技术合同/web-v2.1/R002_OPERATION_CONTRACT.md)，单点 `final_publish_gate()` 覆盖三路径；测试每路径 1 条绕过 payload 断言 0 次 publish。**001 若走 Canonical 下行须显式加入白名单——需 OWNER 决定，故本轮未改。**

### P0-2（已修复）地图子资源可跨版本迁移，含迁入 PUBLISHED
- 现象：四个 PUT 端点只校验源版本 DRAFT，随后 `setattr` 全字段含 `map_version_id`。
- 根因：未比对请求版本与资源当前版本；`parking_slot_id` 无同版本校验；删除车位级联删除点位。
- 修法：`same_version()` 409 `CROSS_VERSION_MOVE_FORBIDDEN`；`slot_in_version()` 422 `SLOT_VERSION_MISMATCH`；删除被引用车位 409 `SLOT_IN_USE`。修复前 5 FAIL，修复后 7 PASS。兼容影响：依赖该迁移行为的客户端将收到 409（仓库内无此用法）。

### P1
| # | 现象 | 根因 | 修法（下阶段） |
|---|---|---|---|
| P1-1 | 发布不计算 `map_versions.checksum`（创建写 `""`） | publish 端点无摘要逻辑 | 发布时计算 SceneManifest digest；旧版本只读回填 |
| P1-2 | ingress 不持久化/比对 location.map_checksum；patrol params 不含 map_checksum | 1.3 字段未纳入快照 | 快照写入并在任务准入比对 |
| P1-3 | `handle_task_status` 不校验 boot_id、不阻止终态回退 | 反馈准入缺失 | 采用 `feedback_applies` 规则 |
| P1-4 | 烟雾无单位定义，前端写死 `%` | 合同未规定 | 车端提供单位前显示“单位待验证”；禁用 300ppm |
| P1-5 | 种子 R002 为 MOCK（`seed.py:532`），而 `REAL_VEHICLE_IDS` 含 R002 | 演示数据与真实身份同名 | SIM 用 `firebot-sim-*`；种子改名需审查（影响 e2e） |
| P1-6 | route_builder/等待区坐标 DEMO_PARKING 写死（`(1.2,1.2)`，前端 `useVehicleOperationState.ts:165` 也写死） | 演示几何进入产品逻辑 | R002 改由已加载场景的 RETURN_HOME/WAITING 点驱动 |
| P1-7 | 地图模块无并发控制；`PUT /maps/{id}` 无版本检查可改 site_id；archive 无审计且不清 active_version_id | 早期实现 | revision 条件更新；补审计 |
| P1-8 | Dock 内巡检/停止/返航前端无权限判定；控制台 1366×768 任务操作在折叠线下 | 资格部分前端推导 | 改为 readiness.permitted_for_user；布局见 DESIGN §3.1 |
| P1-9 | 1.3 无导航 Goal/结果、无场景下发与回执、无防火罩消息 | 协议范围 | schema 1.4 或独立 topic，需车端共审 |

### P2（顺手）
`ManualControl.vue` 未挂载；生成的 TS/Python 协议模型停在 1.2；`services/protocol.py` 缺 schema_version 时默认 1.2；地图模块无 DELETE 点位/轨迹端点。

## 3. 测试命令与结果（隔离栈：PG 19 / Redis 7.2.7 / mosquitto 2.0.11 / Chrome 158 headless，sandbox）

| 命令 | 退出码 | 结果 |
|---|---|---|
| `pytest services/protocol-tester/tests`（含新增 test_contracts_v21） | 0 | 234 passed（新增 107） |
| `pytest apps/api/tests/test_web_v21_map_isolation.py`（修复前，红灯先行） | 1 | 5 failed, 1 passed |
| `pytest test_web_v21_map_isolation + test_api_integration + test_demo_route + test_multi_robot + test_operations_upgrade + test_web_r1_min`（新库 migrate+seed） | 0 | 77 passed |
| 基线对照 `23854b0` 上 `pytest test_web_r1_min`（新库） | 0 | 22 passed |
| `vitest run`（全量） | 0 | 25 文件 / 194 passed（新增 37） |
| `vue-tsc -b --force` / `eslint . --max-warnings=0` / `prettier --check .` | 0/0/0 | 通过 |
| `ruff check` / `ruff format --check`（改动的 Python 文件） | 0/0 | 通过 |
| `scripts/check_docs_links.py` / `check_repository_policy.py` / `check_protocol_drift.py` | 0/0/0 | 82 个 md 链接 OK；策略 OK；1.2 漂移戳未变 |
| `vite build` | 0 | 通过 |
| `node scripts/v21-editor-flow.mjs`（真实浏览器，1366×768 + 1920×1080） | 0 | 18 组断言通过，0 页面错误，0 服务端写入 |
| `node scripts/v21-capture.mjs`（基线） | 0 | 12 张截图，0 页面错误 |

自查发现并修复 1 个测试污染：新地图测试创建站点后未清理，导致同库后续 `test_web_r1_min` 中依赖站点计数的 5 项失败（基线新库 22/22 通过已排除基线问题）；改为模块内自清理后同序 29/29 通过。未执行：完整 `apps/api/tests` 全量（沙箱单次调用 178s 上限，已按相关模块分批覆盖）；Playwright 既有 `e2e/*.spec.ts` 全量（需完整 compose 栈）。

## 4. 需要独立审查的决策
1. 9 个合同字段与状态机（尤其 OperationReadiness action 集、WAITING_OBSTACLE/PAUSED、DeviceOperation 限位确认规则）。
2. dispatcher 最终门禁的语义（emergency_stop 是否受白名单约束；001 白名单）。
3. 场景 DB 迁移 `20261127_0011_scene_v21`（SCENE §6）与旧表映射。
4. 新 MQTT topic（scene / scene_receipt）与 1.4 扩展。
5. 新权限 `robot.control.device`。
