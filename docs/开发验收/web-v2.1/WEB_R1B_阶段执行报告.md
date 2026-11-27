# Firebot V2.1 阶段执行报告 · WEB-R1B（CI 回归 / 合同 draft.3 / 门禁补强 / 0011 地图持久化 / 控制台接线）

## 阶段身份

- ROLE：桌面 CC（Web 全栈 / HMI / 测试）
- STAGE：WEB-R1B
- BASE_SHA：`c56cc4ab040eb5740506fc46ab0ad9aeae691240`（远端 HEAD，已核实）；生产基线仍为 `23854b0…`
- CURRENT_SHA：见证据包 `status.json` 的 `current_sha`
- ENVIRONMENT：沙箱隔离栈：**PostgreSQL 18.4**（与生产同主版本）· Redis 7.2.7 · mosquitto 2.0.11 · API · ingress · dispatcher · task-worker · mock-robot / V1 adapter + 场景化 mock agent · Chrome 158 headless
- CONTRACT_VERSION：`2.1.0-draft.3`（未冻结）

## 实际完成（每批独立提交）

| 批次 | 提交 | 内容 |
|---|---|---|
| CI 修复 | `437b887` | `vehicle-agent-mock.spec.ts:37` 的水平三栏断言与 V2.1 布局冲突，改为 `e2e/helpers/console-layout.ts` 中更强的等价几何验收（1366/1680/1920）。业务链断言未改动。4 个布局变异体在三档分辨率下全部被捕获 |
| 合同 draft.3 | `87e3401` | 6 类错误接受先复现再修复；新增成功确认、readiness 权威来源、冻结范围、最新回执规则 |
| 门禁补强 | `353a73f` | 全局冻结默认开启，SIM 独立放行；不改写历史终态；重启对账；租约切换；只允许 command topic；静态清点所有平台侧发布点 |
| 0011 持久化 | `f76a6f7` `621ee6a` | 迁移、模型、`/api/v1/scenes`、编辑器接线；修复监控地图被草稿顶替的回归 |
| 控制台接线 | `23851a3` | OperationReadiness 统一权威，状态语义修正，`firebot-sim-r002-01` SIM 验收 |
| 收尾 | 本报告提交 | 火警弹窗不再遮挡控制栏；e2e 本地代理在客户端断开时不再退出；证据 |

### draft.2 的 6 类错误接受（修复前后）

| # | 修复前 | 修复后 |
|---|---|---|
| 1 waypoint 0→2 | ACCEPTED_WRONGLY | `WAYPOINT_SKIPPED` |
| 2 新航点复用旧 Goal | ACCEPTED_WRONGLY | `GOAL_REUSED` |
| 3 中间航点 SUCCEEDED | ACCEPTED_WRONGLY | `INTERMEDIATE_SUCCEEDED` |
| 4 snapshot map_checksum ≠ manifest | ACCEPTED_WRONGLY | `NAV_MAP_MISMATCH` |
| 5 未来约 10 分钟的 snapshot | ACCEPTED_WRONGLY | `SNAPSHOT_FROM_FUTURE` |
| 6 OFFLINE/STALE 位姿 + 旧 readiness | ACCEPTED_WRONGLY | `LINK_NOT_ONLINE` / `POSE_NOT_LIVE` / `READINESS_NOT_FROM_THIS_SNAPSHOT` |

## 验证证据（同一最终代码树）

| 命令 | 退出码 | 结果 |
|---|---|---|
| 后端全部 39 个测试文件，分 5 批，每批新建数据库 | 0×5 | 112 + 63 + 69 + 94 + 67 = **405 passed** |
| `pytest services/protocol-tester/tests` | 0 | **338 passed**（合同 211 项） |
| 变异测试：合同 14 / 门禁 6 / 场景 4 / 布局 4 | — | **28/28 被杀死** |
| `scripts/test_migration_matrix.py`（PG 18.4） | 0 | 5/5（含 0010 带数据 → 0011） |
| `alembic downgrade base && upgrade head` + seed | 0 | 通过 |
| vue-tsc / eslint / prettier / vite build | 0/0/0/0 | 通过 |
| vitest | 0 | **209 passed** |
| ruff check + format / docs-links / repo-policy / protocol-drift / migration-manifest | 全部 0 | 通过 |
| e2e `vehicle-agent-mock.spec.ts`（CI 失败项，V1 mock 拓扑） | 0 | **1/1 passed**，三档截图 |
| e2e layout + baseline + multi-vehicle | 1 | **11/12**。唯一失败是 WHEP 媒体用例，需要 MediaMTX，沙箱没有；R1A 同样如此，CI 的 e2e job 有 MediaMTX |
| `v21-editor-flow.mjs` | 0 | 28 组断言：导入→服务端复检→保存→刷新恢复→并发冲突不覆盖 |
| `v21-console-flow.mjs` | 0 | R001：正常、活动任务、离线、冻结（按钮全部禁用）；`firebot-sim-r002-01`：正常、急停锁存（经真实门禁） |

**本轮自查发现并修复的回归**：

1. **监控地图被草稿顶替**：快照取站点中按代码排序的第一个地图，草稿导入后运营地图会消失。已加先红后绿的回归测试，修复前失败、修复后通过。
2. **火警弹窗遮挡控制栏页签**：其他车辆告警时，R1A 把弹窗放在右下角，盖住了右侧控制栏，导致多车用例失败。弹窗已移到地图左下方，布局验收增加控制栏遮挡断言。
3. **0011 外键时机**：设计稿写的 NO ACTION 在 PostgreSQL 上会让整版本级联删除误报违反约束，改为 DEFERRABLE INITIALLY DEFERRED。
4. **e2e 本地代理**：客户端 ECONNRESET 会让代理进程退出，后续用例全部被误判为不可达。仅影响测试工具，已修复。

## 跨端交接

- 合同 draft.3 新增：`waypoint.arrival`、`nav_map_checksum`、`motion`、`stationary`、`source`、`based_on_snapshot_at`、`freeze_scope`。交接包和 `handoff/README.md` 第 6–9 条已更新。
- 车端需要提供：每个航点使用新的 goal 且按顺序上报（经过点也要报）；只在最后一个航点报 SUCCEEDED；实测静止状态；LIVE 位姿必须带 map_checksum。
- 新增 API：`/api/v1/scenes`（导入、读取、保存、删除点位和路线），以及 `GET /robots/{id}/operation-readiness`。
- 不兼容影响：
  - 部署后 `DOWNLINK_FROZEN` 默认为 true，所有 Canonical 下行被冻结，包括软件急停。
  - storage_version=2 的地图版本不能再用旧接口写入。

## 风险及状态

- P0：无新增。
- P1：
  - 场景原子发布、确定性资产摘要、加载回执落库还没做。
  - ingress 的 `task_status` 还没有接入 `apply_feedback` / `confirm_mission_success`。
  - 车位和走廊没有持久化，场景接口对 `parking_slot_code` 一律拒绝。
- P2：WHEP 媒体 e2e 需要 MediaMTX；e2e 登录助手在需改密账号上先探测后改密（既有问题）。
- REAL_CONTROL：NONE（REAL 白名单为空，全局冻结默认开启，001 维持 V1 独立冻结）
- PRODUCTION_MUTATION：NONE（未部署、未执行生产迁移、未改 ACL、未恢复 dispatcher）

## 下一阶段

- 最小实施建议：
  1. 0011 第二批：原子发布与场景摘要，加载请求和回执落库（只在 SIM 验证）。
  2. ingress 接入 draft.3 的反馈与成功确认规则（只在 SIM）。
  3. 车位持久化。
- 需要独立审查：draft.3 字段；冻结默认值；DEFERRABLE 外键；读取接口的错误码语义。
- 需要 OWNER 授权：生产迁移 0011；部署默认冻结的版本；任何 REAL 白名单或解冻；远端推送。
