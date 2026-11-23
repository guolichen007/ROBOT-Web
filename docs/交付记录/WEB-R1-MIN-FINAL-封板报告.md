# WEB-R1-MIN-FINAL 定点修复与封板报告（PC）

- 基线：`1de3612`；上一候选：`7ec2b99`（CI Run 45126853712 / Attempt 1：7 成功、1 失败（containers：Trivy 检出 nginx CRITICAL）、2 跳过（e2e、vehicle-agent-container-e2e 依赖 containers））。
- 范围：只做 PC 网页端；移动端不再开发，已有响应式样式保留不动。
- 以下各项全部为本机实测结果；同 SHA 的 CI 结果以推送后为准。

```
PRODUCTION_MUTATION=NONE
REAL_VEHICLE_COMMANDS=0
DO_NOT_TOUCH：车辆控制协议 / 导航 / 防火罩驱动 / command-dispatcher / vehicle-agent adapter / G1·G2·G4 实车通信 —— 零改动
```

## P0-01 Web 镜像安全 — NGINX_CRITICAL_ZERO

| 项目 | 内容 |
|---|---|
| 现象 | containers 作业 Trivy 门禁失败 |
| 根因 | `nginx:1.30.4-alpine` 中 nginx 1.30.4-r1 有 5 个 CRITICAL：CVE-2026-113425 / 121810 / 123007 / 123633（修复于 1.30.7-r0）、CVE-2026-97205（修复于 1.30.6-r0）。brotli-libs CVE-2026-111497 已由原 apk 修复行覆盖 |
| 修法 | `FROM nginx:1.30.7-alpine3.24@sha256:caf4e1e032a20f9c3c5e28fc7f25ffd768ad8677b4975c54193075997b3a97e3`（alpine 3.24.4，nginx 1.30.7-r1，brotli-libs 1.2.1-r0）。保留 brotli 修复行；Trivy 门禁、严重级别、忽略规则都没有改动 |
| 同步 | 同一漏洞版本的 ingress nginx（compose.dev / compose.test / docker-compose.server / u-dao ingress README）固定到相同 digest；离线拉取、加载脚本和部署 manifest 第三方镜像清单同步标签 |
| 证明 | 用与 CI 同版本的 Trivy 0.59.1（从 `ghcr.io/aquasecurity/trivy:0.59.1` 中提取）扫描：旧镜像复现 CRITICAL×6（nginx 5 + brotli 1）；新最终镜像 rootfs（含 dist + nginx.conf）`--exit-code 1 --severity CRITICAL --ignore-unfixed` 结果为 **Total: 0**，去掉 `--ignore-unfixed` 后也是 0。剩余 HIGH×1（libxml2 CVE-2026-86144）不触发门禁，记录在案 |
| 局限 | 本机没有 Docker，扫描对象是镜像层解包后的 rootfs，而不是 `docker build` 产物；最终以 CI containers 作业为准 |

## P0-02 新账号默认角色 — NEW_USER_DEFAULT_VIEWER

- `UsersView.vue` 的默认角色由 `operator` 改为 `viewer`；角色列表为空时，前端和后端都拒绝提交。
- 浏览器断言：打开"新增账号"抽屉，不动角色勾选 → 勾选项严格等于 `['viewer']` → 创建 → 请求体 `role_codes=['viewer']` → 数据库里的有效角色为 `['viewer']`，权限严格等于 `alarm.read, map.read, patrol.report.read, robot.read`。
- 后端：不传 `role_codes` 时默认 viewer；传空数组返回 422。授予其他角色仍要经过服务端 `assert_can_grant` 校验。

## P0-03 浏览器验收断言 — E2E_ASSERTIONS

- `browser-flow.mjs` 全部改为 `node:assert/strict` 断言，**67 项**；任何一项失败都会导致进程非零退出。全部负测都校验 HTTP 状态、错误码以及业务数据没有被改动。
- **确认的是同一个火警**：弹窗带 `data-alarm-id` / `data-event-code` 属性。断言内容：请求 URL 必须是 `/alarms/{弹窗 id}/acknowledge`；数据库中该事件变为 ACKNOWLEDGED，event_code 与弹窗显示一致，记录了确认人；另一条未确认火警仍为 NEW。
- **VERDICT**：Command、Outbox、Outbox 已发布、Task 四项增量必须都为 0，且浏览器没有监控通道以外的 WebSocket，否则退出码为 1。
- **反证**（证据见 `evidence/fault/`）：
  - `FAULT=status`：把第一个负测的期望值改成 200 → `ASSERTION FAILED [NEG_WRONG_ROLE]`，**rc=1**
  - `FAULT=command`：向隔离库写入 1 条 Command → `ASSERTION FAILED [VERDICT_NO_COMMAND]`，**rc=1**

## P0-04 历史计划执行边界 — LEGACY_SCHEDULE_PROTECTION

| | PREVIEW_ONLY（本轮 Web 新建的计划） | LEGACY_EXECUTABLE（迁移前已存在的计划） |
|---|---|---|
| 来源 | `/plans` 以及旧接口 `POST /patrol-plans`（现在只能建 PREVIEW_ONLY） | 迁移 0010 的 server_default |
| enabled | 恒为 false（DB CHECK `ck_patrol_plan_preview_not_enabled`） | 保持原值 |
| 生成 Task/Command | 四处都拒绝：`build_patrol_task` / `build_resumed_patrol_task` / `/tasks/patrol-plan` / task-worker | 行为不变，仍受原有门禁约束（robot ONLINE、控制合同和 ACK 合同已验证、地图合同、readiness） |
| 调度 | enabled=false、next_run_at=NULL；worker 查询会排除它；人为篡改到期也会被拒绝 | 不变 |

- 只读核查：`python scripts/web-r1-min/legacy_plan_audit.py [--json]`，在 `SET TRANSACTION READ ONLY` 下只做 SELECT。测试证明该事务内执行 UPDATE 会被数据库拒绝。
- 测试库核查结果：LEGACY_EXECUTABLE 1 个（seed 中的 `RIGHT_SIDE_S_CRUISE_PLAN`，enabled=true，**没有调度，auto_dispatch_possible=0**）。它只能由人工在监控页"开始巡逻"触发，并且仍要通过 readiness、等待区和控制合同门禁。
- **正式发布保护说明**：
  1. 部署前在生产库用只读账号运行上述脚本，将 `legacy_enabled` 和 `auto_dispatch_possible` 列入发布记录；
  2. 本轮**不修改、不停用**生产历史计划；
  3. 若 `auto_dispatch_possible>0`，必须由 OWNER 决定是否先在现场停用对应调度，再发布；
  4. 车端下行开关（`VEHICLE_AGENT_V1_DOWNLINK_ENABLED` / allowlist）也一并输出到核查结果中，供复核。

## P1-01 计划并发修改 — PLAN_CONCURRENCY

- 根因：原来先比较 revision、再在 commit 时写入，两个请求之间存在竞态窗口。
- 修法：用 `UPDATE patrol_plans SET revision=revision+1 WHERE id=? AND revision=?` 作为提交点，影响行数为 0 时返回 409。PostgreSQL 下后到的事务会在行锁上等待，前一个事务提交后重新求值 WHERE。
- 测试：两个线程以同一 revision 并发保存，结果严格为 `[200, 409]`，最终 revision=2；另有一个"读取后被他人提交"的确定性用例返回 409。
- 反证：把代码临时退回旧逻辑后，并发用例失败（两个请求都成功）。

## P1-02 审计范围 — AUDIT_SCOPE

- 根因：`audit_rows` 对 `robot_id IS NULL` 的记录一律放行，普通审计员能读到所有人员和认证类审计；`/admin/audit` 完全没有范围限制。
- 修法：新增 `audit_scope_filters`。持 `settings.manage` 者不受限；其他账号只能看本站点车辆相关的记录，以及自己本人的操作。`/history/audit`、`/history/operations`、导出、`/admin/audit` 统一使用这一规则。
- 测试：auditor 在两个接口和导出中都看不到其他账号的 USER_* / AUTH_* 记录；管理员可以看到。

## P1-03 运营站点配置 — OPERATING_SITE_CONFIG

- 存在多个启用站点却没有设置 `OPERATING_SITE_CODE`，或配置的站点不存在、未启用时：普通账号的 API 返回 **503 `OPERATING_SITE_MISCONFIGURED`**，WS 连接直接关闭，不会把普通用户静默分配到第一个站点。
- 管理员不受影响，可以修复配置；`/system/status` 的 `checks.operating_site` 会显示具体原因；应用启动时会记录错误日志。
- 单站点且未配置时，使用唯一的那个站点；没有启用站点时，普通账号的范围为空。

## P1-04 数据库回滚 — DATABASE_ROLLBACK

演练脚本 `scripts/web-r1-min/rollback_drill.sh`（只使用临时库，用 PG 18.7 的 pg_dump / pg_restore）实测结果：

```
STEP1 B0009 备份（发布前 0009 结构）
STEP2 升级 0010，经 handle_alarm 写入 MATCHED×2 + UNMATCHED×2，fire=4:8263ba63…
STEP3 B0010 备份
STEP4 DOWNGRADE_REFUSED=PASS：存在 UNMATCHED 时 downgrade 失败；版本仍为 0010；火警行数和内容哈希不变
STEP5 RESTORE_B0010=PASS：恢复后内容哈希一致
STEP6 RESTORE_B0009=PASS：恢复为 0009，之后可以再次 upgrade head
STEP7 导出 UNMATCHED 火警 CSV 共 2 行，库内仍保留 2 行（没有删除任何火警）
ROLLBACK_DRILL=PASS
```

**生产回滚步骤**：
1. 发布前执行 `pg_dump -Fc` 做全库备份，并记录当前镜像 digest；
2. 只回滚应用：把 api / web / task-worker / mqtt-ingress 镜像切回 `1de3612` 对应的 digest。0010 新增的列和约束对旧代码是加法变更，但旧代码在 FireEvent 上没有 `parking_slot_id` 可空的语义，因此**有 UNMATCHED 火警时不应只回滚应用**；
3. 回滚数据库：先导出 UNMATCHED 火警并归档（第 7 步的 SQL）；默认按第 5 步从发布前备份 B0009 恢复，会丢失发布后产生的数据；需要保留发布后数据时，必须由 OWNER 决定如何处置 UNMATCHED 记录后，再执行 `alembic downgrade 20261021_0009`；
4. **禁止**为了让降级成功而删除火警。

## PC 界面验收 — PC_BROWSER_SCREENSHOTS

`scripts/web-r1-min/pc-pages.mjs` 覆盖 3 种分辨率 × 10 个页面，共 **30 张原始 Chromium 截图**（`evidence/pc/`）。自动检查：禁用按钮必须有原因、label 与输入控件一一对应、抽屉内按钮没有被遮挡、火警弹窗不遮挡标题和主按钮、没有横向滚动。结果 **30/30 无问题**。页面包括：监控首页、巡检计划列表 / 表单 / 详情、任务记录、火警列表 / 处置详情、用户列表 / 新增账号、记录与审计。

本轮通过截图发现并修复的问题：

| 问题 | 修复 |
|---|---|
| 火警弹窗（z 400）盖住抽屉（z 300）的"保存 / 取消"按钮 | 弹窗 z 改为 290；弹窗显示时，页面（监控首页除外）底部留出 240px |
| 1366×768 和 1440×900 下长表单的"保存草稿"需要滚动才能看到 | 抽屉操作栏改为 sticky，始终可见 |
| 分页"上一页 / 下一页"禁用时没有原因 | 增加 title："已是第一页 / 最后一页" |
| "保存草稿"禁用时没有原因 | title 显示阻断原因（例如"请选择执行车辆"） |
| 监控首页：本车火警已有横幅和主火警面板，全局弹窗重复出现并遮挡控制栏 | 在监控页不再弹出当前车辆的火警（站点级和其他车辆的火警照常弹出）；E2E 断言 `MONITOR_BANNER_NOT_DUPLICATE_POPUP` |
| **1366×768 监控首页布局缺陷（基线 1de3612 即存在）**：两列网格放三块面板，控制栏被挤到第二行左侧，右下角空白，地图只有约 230px 高，车位标签互相重叠 | 地图跨两行，视频和控制栏叠放在右侧（地图 703×471）；修复前后对比见 `evidence/mon-base.png` / `mon-fix.png` |

安全控制按钮（软件急停、启动 / 关闭、巡逻、返航）继续保持原有的安全门；页面上已有原因提示（"急停合同未验证""未绑定 vehicle_agent v1"）。

## 本地门禁（候选 HEAD 全量）

| 门禁 | 结果 |
|---|---|
| ruff / format / mypy app / mypy services ×12 | PASS |
| migration manifest、matrix 4/4、downgrade base → upgrade | PASS |
| pytest：182 + 263（1 skip）+ 11 | PASS（test_web_r1_min 23 例） |
| web：prettier、eslint、vue-tsc、vitest 23 文件 157 例、build | PASS |
| docs / policy / server surface / image isolation / protocol drift / rollback | PASS |
| browser-flow 67/67 断言；pc-pages 30/30；FAULT=status、FAULT=command 均 rc=1 | PASS |
| Trivy 0.59.1 最终 Web 镜像 CRITICAL | 0 |

本机未执行（没有 Docker）：containers 的 docker build、compose 全栈 e2e、vehicle-agent-*-e2e。这些以 CI 结果为准。
