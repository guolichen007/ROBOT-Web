# WEB-R1-MIN 研发交付报告（最小企业化功能闭环）

- 基线：`origin/feature/g2-g3-integration` HEAD `1de3612`（CI Run 44469641378 / Attempt 1，10/10 success，开工前经 GitHub API 核实）
- 研发分支：`feature/web-r1-min`（3 个阶段提交，见文末）
- 状态：**研发完成，可部署候选**；未部署、未推送生产。CI 同 SHA 同 Run 同 Attempt 结果需推送后核对（见“未完成项”）。

```
PRODUCTION_MUTATION=NONE
REAL_VEHICLE_COMMANDS=0
DO_NOT_TOUCH: 车辆控制协议 / 导航算法 / 防火罩驱动 / command-dispatcher / vehicle-agent adapter /
              G1/G2/G4 实车通信 —— 零改动（commands/* 仅 find_robot 增加 auth 参数）
```

## 1. 阶段一：账号权限与明显缺陷

| 项目 | 现象（基线） | 根因 | 修法 |
|---|---|---|---|
| 账号列表泄露口令哈希 | `GET /admin/users` 返回 `password_hash` | `serialize_model(User)` 序列化全部列 | `public_user()` 白名单输出 |
| 越权授予 | 有 `user.manage` 即可创建 super_admin | `create_user` 直接写入 `role_codes` | `assert_can_grant`：super_admin 仅 super_admin；administrator 需 user.manage+role.manage；无 role.manage 只能建只读 |
| 自我提权 / 最后管理员 | 可改自己角色、可停用最后管理员 | 无规则 | 禁止改自身角色/状态/重置自己密码；`active_admin_count` + 行锁串行化，最后管理员降级/停用返回 `LAST_ADMIN_PROTECTED` |
| 权限变化后旧会话越权 | 改角色只撤刷新会话，15 分钟内访问令牌、已连 WS、媒体票据仍有效 | JWT 无版本 | `users.token_version` 写入访问令牌 / WS 票据 / 媒体票据；角色/状态/密码变化 +1；WS 每 2 s 复核后 `session_revoked` + 4401 |
| 停用不完整 | 无停用、资料修改、重置密码接口 | 缺 API | `PATCH /users/{id}`、`PUT /users/{id}/status`、`POST /users/{id}/reset-password`；逻辑停用保留历史 |
| 422 返回 500 | 字段校验 ValueError 时 500，且可能回显输入（密码） | `exc.errors()` 含不可序列化 ctx 和 input | 只回 type/loc/msg |
| 跨站点越权 | 普通账号可读任意站点车辆/告警/媒体/历史 | 无范围约束 | `core/scope.py` 运营站点；robots/snapshot/tasks/alarms/commands/media/reports/history/export/WS 全部收口，越权 404 |
| 新建计划永久禁用 | `<t-button disabled>新建计划</t-button>` | 硬编码 | 仅无权限/无车辆时禁用并显示原因 |
| 任务页默认取第一个车位/轨迹 | `parking_slots[0]` / `trajectories[0]` 直接创建巡检 | 代替用户决定 | 删除该入口；任务页只做筛选+详情 |

单站点规则：持 `settings.manage` 者不受限；其他账号只看 `OPERATING_SITE_CODE`（缺省=首个 active Site，与监控快照选择一致）。**不是通用多租户框架**。

## 2. 阶段二：五项业务功能

1. **巡检**：`/api/v1/plans` 创建/编辑（revision 乐观锁）/列表/详情/暂停/恢复草稿/时间预演/预演记录。复用 PatrolPlan + PatrolSchedule。服务端强制 `PREVIEW_ONLY` + `enabled=false`（DB CHECK `ck_patrol_plan_preview_not_enabled`）；调度 `enabled=false`、`next_run_at=NULL`；`build_patrol_task` / `build_resumed_patrol_task` / `/tasks/patrol-plan` / task-worker 四处硬拒绝；`/plans/{id}/enable` 409 + 审计；旧 `POST /patrol-plans` 也不能再建可执行计划。
2. **右侧作业**：`work_side=RIGHT`（CHECK）、侧向间距 0.20–0.30 m（CHECK），恒显示“未验证”；右侧防火罩机构只展示登记状态（`NOT_VERIFIED`、`control_enabled=false`）。参数不进入任何命令 payload。
3. **火警**：顶栏通知入口 + 全局弹窗（查看详情 / 确认收到 / 关闭提示三者分离）；处置详情（处置记录、时间线、备注）。修复未匹配车位丢失（保存为 `UNMATCHED`，禁止自动派发灭火）与已解决事件吞并新上报（终态事件不再合并）。无实时检测通道时显示“检测数据未接入”，NORMAL 态势文案改为“车辆状态有效”。
4. **记录**：任务 / 计划预演 / 告警 / 人员操作 / 审计 五类筛选、详情；`/history/export/{kind}` CSV 与页面同一过滤函数；只读用户不可导出；公式注入防护；导出与拒绝均审计。
5. **页面**：通用 DataTable 加载/错误重试/空态/分页；筛选栏、抽屉；≤760 px 移动端布局。未解除任何安全控制禁用。

## 3. 阶段三：全流程验证（真实浏览器）

环境：headless Chromium 1248（chrome-headless-shell）+ 真实 uvicorn API + PostgreSQL 18.4 + Redis 7.2，同源代理；**未启动任何 MQTT Broker / dispatcher / adapter**。火警以进程内 `mqtt-ingress.handle_alarm` 注入（`media.label=SIMULATED`，不经 Broker）。脚本：`scripts/web-r1-min/browser-flow.mjs`，连续两次 52 步全通过。

| # | 步骤 | 结果 |
|---|---|---|
| 1 | 管理员登录（首次强制改密） | PASS |
| 2 | 新增操作员（初始只读） | PASS |
| 3 | 分配角色 只读→操作员（旧会话失效提示） | PASS |
| 4 | 操作员登录（首次改密→重登） | PASS |
| 5–7 | 创建右侧巡检计划 → 预览时间（每周一/五 08:30，5 次）→ 保存草稿 | PASS；PREVIEW_ONLY / enabled=false / RIGHT / 0.20–0.30 / NOT_VERIFIED |
| 8 | 查看任务记录（无“创建巡检任务”入口，未新增任务） | PASS |
| 9 | 接收隔离火警（未知位置）→ 弹窗显示“位置未匹配” | PASS |
| 10 | 确认收到（弹窗）+ 处置详情 | PASS |
| 11 | 查询操作审计（角色/计划/确认收到可查）+ 导出 CSV | PASS |
| 12 | 管理员停用账号 | PASS |
| 13 | 会话失效：页面回登录、旧令牌 401、重新登录 403“账号已停用” | PASS |

**零下行证明**（流程前后计数）：`commands +0, outbox +0, outbox_published +0, tasks +0`，浏览器无 `/ws/v1/monitor` 以外的 WebSocket。MQTT 下行唯一入口是 Command→OutboxEvent→dispatcher，两表零新增即零下行。

### 负向用例覆盖

| 要求 | 证据 |
|---|---|
| 权限越权 | E2E `NEG_PRIVILEGE_ESCALATION 403`、`NEG_OPERATOR_USER_ADMIN 403`；pytest `test_role_grant_limits_self_escalation_and_unknown_role`、`test_cross_site_api_ws_media_and_export_blocked` |
| 错误角色 | E2E `NEG_WRONG_ROLE 400 ROLE_UNKNOWN` |
| 重复保存 | E2E `NEG_DUPLICATE_SAVE 409`；pytest 同幂等键重放返回同一计划 |
| 无路线 | E2E `NEG_NO_ROUTE 422`；vitest “尚未配置路线”且不可保存、不自动选路线 |
| 无车辆 | E2E `NEG_NO_VEHICLE 422`；vitest 无车辆时“新建计划”禁用并说明原因 |
| 计划误启用 | E2E `NEG_PLAN_ENABLE 409`、`NEG_CREATE_ENABLED 409`、`NEG_RUN_PREVIEW_PLAN 409 PATROL_PLAN_PREVIEW_ONLY`；pytest DB CHECK、worker 篡改到期调度仍 `REJECTED/PREVIEW_ONLY` |
| 未知位置火警 | E2E 两条 `UNMATCHED`；pytest 不可自动派发灭火 `ALARM_LOCATION_UNMATCHED` |
| 重复事件 | E2E 同 event_id 合并为 `occurrence_count=2`；pytest 已解决后再上报新建事件 |
| 断网重连 | E2E 浏览器 `setOffline(true)` 期间注入火警，恢复后弹窗 + 通知入口计数 2 |
| 未确认火警 | E2E “关闭提示”后状态仍 `NEW`，剩余未确认 1 |
| 导出权限 | E2E 操作员导出审计 403、导出任务 200；pytest viewer 403、auditor 导出行数=页面行数 |
| 禁用账号 | E2E 步骤 12–13；pytest WS `session_revoked`、媒体票据 403、ws-ticket 401 |

### 截图（`evidence/shots/`）

桌面 1440×900：02 新增账号、03 分配角色、05 计划表单+预览、07 计划列表、07b 计划详情、08 任务记录、09 火警弹窗、10 火警处置详情、11 操作审计、12 停用账号、13 停用后登录被拒。移动 390×844：m1 巡检计划、m2 告警、m3 用户、m4 记录、m5 任务、m6 计划表单。

## 4. 本地门禁（候选 SHA 全量）

| 门禁 | 结果 |
|---|---|
| ruff check / format、mypy app、mypy services/*/main.py ×12 | PASS |
| check_migration_manifest（10 revisions）、test_migration_matrix（4 起点→0010） | PASS |
| alembic downgrade base → upgrade head → seed | PASS |
| pytest apps/api/tests（34 文件，含新增 16）+ protocol-tester + secret_redaction + r001-independent | PASS |
| web：eslint、prettier --check、vue-tsc、vitest 23 文件 156 例、vite build | PASS |
| docs：check_docs_links、check_repository_policy、check_server_surface、check_test_image_isolation、check_protocol_drift、test_server_rollback | PASS |

未在本机执行（无 Docker）：containers、e2e（compose 全栈 Playwright）、vehicle-agent-*-e2e、ros1-vehicle 的 catkin FIELD gate。以 CI 结果为准。

## 5. 已知风险与边界

- 迁移 0010 降级：存在 `UNMATCHED` 火警时 **fail-closed** 拒绝降级（不静默删除事件）；回滚前需先导出/归档这些记录。
- 历史计划标记为 `LEGACY_EXECUTABLE`，原行为不变；本页面对其只读。
- 侧向间距、右侧防火罩均为配置/展示，**未经实测，界面恒显示“未验证/未验收”**。
- 运营站点缺省取首个 active Site；多站点部署前必须显式配置 `OPERATING_SITE_CODE`。
- 旧前端 E2E `multi-vehicle.spec.ts` 仍直接调用 `POST /tasks/patrol`（遗留可执行计划），不受本轮影响。

## 6. 未完成项（需 OWNER / 云端）

1. 推送 `feature/web-r1-min`（沙箱无 GitHub 推送凭据），核对 **同 SHA 同 Run 同 Attempt** 10/10。
2. 云端 CC 独立 Preview 部署与运行验收（不触碰生产 Broker/实车链路）。
