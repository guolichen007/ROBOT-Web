# Firebot V2.1 阶段执行报告 · WEB-R1A（核心合同 HF1 / 最终发布门禁 / 综合控制台首版）

## 阶段身份

- ROLE：桌面 CC（Web 全栈 / HMI / 测试）
- STAGE：WEB-R1A
- BASE_SHA：`1134dc8ee6372f0f4a94d6ee7251b766e23f471b`（= origin/feature/firebot-r002-web-v2-1，2026-11-27 核实）；生产基线仍为 `23854b02cd32f4e36a88826ad2293d9c623c8f38`
- CURRENT_SHA：见证据包 `status.json` 的 `current_sha`（本报告所在提交）
- ENVIRONMENT：沙箱隔离栈：PostgreSQL 19 · Redis 7.2.7 · mosquitto 2.0.11 · API · ingress · dispatcher · task-worker · mock-robot · Chrome 158 headless。仅测试栈放行 mock 车 `R001,R002` 的 Canonical 下行
- CONTRACT_VERSION：`2.1.0-draft.2`（未冻结）

## 实际完成

| 优先级 | 内容 | 提交 |
|---|---|---|
| 1 | C1–C3 核心合同 HF1：五项审查反例修复；执行代次 / command 关联 / 合法迁移 / 新鲜度 / 重放；阈值精确边界；core / extension 分层；VM 交接包 `handoff/` | `077f5fa` |
| 2 | 最终发布门禁：`services/dispatch_gate.py` 单点覆盖 Outbox / Redis 人工控制 / 安全 Stream；REAL 默认拒绝；001 V1 分支不变 | `dee871f`、`0abd909` |
| 3 | WEB-1 综合控制台首版：紧凑状态栏（四维独立）、地图主导、双视频、底部固定操作区、保护停车入口（只呈现服务端资格）、手动控制防越权 | `5cd8b5f` |
| 预备 | 编辑器：空输入不再变 0、HOME 不入默认巡检路线、错误可关闭、火警弹窗不遮挡；0011 最小数据模型与迁移设计（仅设计） | `13a6ef6` |

### 五项反例（修复前后）

| 反例 | 1134dc8（修复前） | 修复后 |
|---|---|---|
| CE1 已绑定 Goal 接受空 Goal 反馈 | ACCEPTED_WRONGLY | `GOAL_REQUIRED` |
| CE2 EXECUTING 回退 ACCEPTED | ACCEPTED_WRONGLY | `ILLEGAL_TRANSITION` |
| CE3 回执未绑定车辆 / 加载操作 | ACCEPTED_WRONGLY | `RECEIPT_VEHICLE_MISMATCH` / `RECEIPT_LOAD_MISMATCH` |
| CE4 过期 readiness 通过准入 | ACCEPTED_WRONGLY | `READINESS_EXPIRED` |
| CE5 ARCHIVED 场景创建新任务 | ACCEPTED_WRONGLY | `SCENE_NOT_ACTIVE` |

## 验证证据

| 命令 | 退出码 | 结果 |
|---|---|---|
| `pytest services/protocol-tester/tests` | 0 | 299 passed（含合同 172 项） |
| HF1 变异测试（11 个变异体） | — | 11/11 被杀死 |
| `pytest test_web_r1a_final_publish_gate.py` | 0 | 27 passed；每项断言捕获客户端 0 次 publish |
| 门禁变异测试（11 个变异体） | — | 11/11 被杀死（关闭门禁时 21 项失败） |
| 后端全部 37 个测试文件，分 5 批、每批新库 | A/B/C/E=0；D=1 → D2=0 | A 69、B 63、C 69、E 67；D 见下，D2 共 94 passed |
| `vitest run` / `vue-tsc` / `eslint` / `prettier` / `vite build` | 0/0/0/0/0 | 204 passed |
| `ruff check` + `ruff format --check`（本轮改动的 Python） | 0 | 通过 |
| `check_docs_links` / `check_repository_policy` / `check_protocol_drift` | 0/0/0 | 通过 |
| 既有 e2e `current-monitor-layout.spec.ts` | 0 | 2/2 passed（新控制台下） |
| 既有 e2e `baseline.spec.ts` + `multi-vehicle.spec.ts` | 1 | 9/10 passed；WHEP 用例需 MediaMTX，沙箱不可执行 |
| `v21-console-flow.mjs`（正常 / 活动任务 / 故障 / 离线 × 1366、1920） | 0 | 几何与状态断言全部通过 |
| `v21-editor-flow.mjs` | 0 | 24 组断言（含真实火警弹窗下的遮挡断言） |

**批次 D 说明**：共 4 项失败。
- 1 项是本轮新写断言依赖环境：测试栈放行 R002 时拒绝码为 `FG_UNAUTHORIZED` 而非 `FG_NOT_ALLOWLISTED`。已改为两种拒绝都接受（`0abd909`）。
- 另 3 项是 `test_v1_readonly_registration` 在新库上返回 428（管理员需先改初始密码），在 `1134dc8` 上复现完全相同，属既有的顺序依赖。前置 `test_web_r1_min` 后整批 94/94 通过。

**自查纠错**：
- `1134dc8` 的 `v21-editor-flow.mjs` 和 `5cd8b5f` 的 `console-v21.test.ts`、`v21-console-flow.mjs` 都是在跑完门禁之后又修改、未重跑就提交的，导致 lint/typecheck 失败。已分别在 `5cd8b5f` 和 `13a6ef6` 修正。
- 本轮最后一次提交前，已在完整代码树上重跑全部门禁。

## 跨端交接

- 新增或变更合同：
  - 新增 `SceneLoadRequest`。
  - `SceneLoadReceipt` 新增必填 `load_id`。
  - `MissionRequest`、`MissionFeedback` 新增必填 `command_id` 和 `execution_gen`。
  - `MissionFeedback` 的 `waypoint_index`、`waypoint_total` 改为必填；`EXECUTING` / `WAITING_OBSTACLE` / `SUCCEEDED` 必须带 `goal_id`。
  - `VehicleSnapshot` 的烟雾、温度、设备字段改为可选。
- 所需其他 Agent 输入（VM CC，见 `handoff/README.md` 中的 Q1–Q6）：goal_id 来源、map_checksum 算法、场景 topic 形式、每航点一个 goal 还是整条路线一个、加载失败原因码、障碍等待判定。
- 已提供机器可读样例：
  - `vectors/*.vectors.json` 中的 `apply_cases`、`confirm_cases`、`admission_cases`、`threshold_cases`。
  - `handoff/handoff.json`，包含各文件 SHA256。
- 不兼容影响：
  - draft.1 的反馈与回执样例需要补上新的必填字段。
  - **部署门禁后、未配置 `CANONICAL_DOWNLINK_ALLOWLIST` 前，所有 Canonical 下行（含 R002 停止、急停、任务）都会被拒，状态显示 `GATE_REJECTED`**。这符合当前 REAL 控制 NO-GO 的预期。
  - 001 走 V1，不受影响。

## 风险及状态

- **P0**：无新增。门禁已在候选分支实施，**待独立审查**。
- **P1**：
  - 前端仍用 DEMO 等待区坐标 `(1.2,1.2)` 推导「已在等待区」；该逻辑只会多拒绝、不会放行，但违反「资格只来自服务端」。
  - ingress 未持久化 `map_checksum`，`task_status` 未接入 `apply_feedback`。
  - 控制台「通信」维度与 KPI 「智能车状态」的陈旧阈值来源不同（integration 阈值与 agentFreshness 8 s）。
  - `test_v1_readonly_registration` 存在既有顺序依赖。
- **P2**：e2e 登录助手在需改密账号上先探测目标、后改密，导致 428；地图车位标签在 1366 宽度下挤叠（基线即有）。
- 未完成事项：
  - 保护停车与防火罩的服务端能力及下行。
  - `OperationReadiness` API 端点（控制台已预留读取）。
  - 0011 迁移实施。
  - 编辑器服务端保存、发布、加载。
  - 车云 SIM 联调（等 VM）。
  - WHEP 媒体 e2e（需 MediaMTX）。
- REAL_CONTROL：NONE（未授权远程命令一律 NO-GO；生产默认 Canonical 白名单为空）
- PRODUCTION_MUTATION：NONE（未部署、未改真实车辆配置、未推送；PREVIEW_ONLY 保护未动）

## 下一阶段

- 最小实施建议：
  1. 后端提供 `OperationReadiness` 端点，复用 `robot_readiness`，并加入 `control_frozen` 和 `permitted_for_user`；控制台改为只读这一来源，并移除前端 DEMO 等待区推导。
  2. 按设计实施 0011 的第 1 步（迁移、模型、矩阵测试）。
  3. ingress 的 `task_status` 接入 `apply_feedback`（只在 SIM 验证）。
- 需要独立审查的决策：
  - draft.2 的字段与状态迁移表。
  - 门禁的检查顺序与拒绝语义，emergency_stop 是否也受白名单约束（当前受约束）。
  - SIM 凭证判定目前只依据生产 Broker 主机名。
  - 0011 的复合外键方案。
- 需要 OWNER 专项授权的操作：
  - 生产环境 `CANONICAL_DOWNLINK_ALLOWLIST` 的任何取值。
  - 部署包含门禁的版本（部署后 R002 的 Canonical 下行全部被拒）。
  - 远端推送。
