# ROBOT-Web UI Gate-4 收口报告

分支 `ui/youdao-light-hmi-v1`，起点 `origin/ui/youdao-light-hmi-v1` @ `16b7881`，无 hard reset。

## 新 HEAD

```
c14e779 test(web-ui): align gate4 geometry assertions with telemetry consolidation
```

## 提交（6 个）

| SHA | 内容 |
|---|---|
| `007c23d` | `feat(web-ui): add gate4 generated hmi art assets` |
| `32c5e1a` | `feat(web-ui): complete Chinese localization across operator views` |
| `f519faa` | `refactor(web-ui): consolidate monitor telemetry and status cards` |
| `95372e9` | `fix(web-ui): keep extinguish actions interactive and close dock feedback loop` |
| `4cb2840` | `chore(platform): add safe development demo-data cleanup workflow` |
| `c14e779` | `test(web-ui): align gate4 geometry assertions with telemetry consolidation` |

## 硬目标落点

### A 全站中文化 ✅
- 页面 eyebrow 全部中文（任务调度/火情生命周期/审计日志/历史回放/地图版本/车位与操作点/设备管理/系统状态/用户与权限）。
- 新增 `reasonCodeLabel`（ROBOT_ESTOP_ACTIVE→机器人处于急停状态 等）、`auditActionLabel`、`robotModeLabel`；`taskTypeLabel` 改为「巡检任务/灭火任务/前往预设点」。
- `/tasks` 类型、`/alarms` 类型/来源/严重度、`/audit` 动作、`/settings` 部署就绪、`/users` 弹窗 eyebrow 全部中文。
- 模板裸英文扫描（EXECUTION POLICY / ACTIVE / SERVER DEPLOY / CREATE USER / ROBOT_ESTOP_ACTIVE …）结果为空。

### B 顶栏集中遥测 ✅
- Topbar 新增 顶部热像 / 底部红外 / 烟雾浓度 / 数据更新 四个 compact 指标（`robot.top_ir/bottom_ir/smoke` + freshness），未接入显示 `--`。

### C 正常态右栏 ✅
- 右栏改为：实时视频 → 设备快照（`DeviceSnapshot.vue`：编号/模式/任务/任务编号/定位/右侧检测/视频状态/更新）→ 巡检状态。
- 删除了重复的 `RiskTelemetryRibbon` 组件及其 4 个大卡。

### D 火警态右栏 ✅
- 右栏改为：实时视频 → `PrimaryAlarmPanel`（详情+三动作）→ 其他事件（折叠），不再渲染环境大卡。

### E 三个灭火动作始终可点 ✅
- `ExtinguishActionCards` 只在 `busyMode`（请求进行中）时灰掉；readiness/只读/冲突不再让按钮无反馈灰掉，只作为 hint 展示。
- 点击立即 create-task；后端拒绝时 `friendlyError()` 用 `reasonCodeLabel` 转中文提示；RESOLVED 仍被后端 409。

### F 底部命令闭环 ✅
- Dock 四按钮改为 `busy` 驱动（不再用 readiness 静默灰掉）：点击 loading（「下发中…」）、成功 toast + `loadSnapshot()` 刷新、失败中文错误。
- 停止巡检 poll `StopOperation`，最终态后主动 `loadSnapshot()`；急停仍 0.8s hold。

### G 巡检状态视觉 ✅
- 巡检卡接入 `card_bg_status_glow_*` + `robot_state_*_art` + 新 `ProgressRingGate4` 组件；进度只绑真实 `Task.progress`，不写 93%/42/54/02:35:18。

### H 模拟数据清理 ✅（方案 + 脚本，未执行）
- 审计结论：无统一 `source_kind` 判别字段；可靠判别为 `RobotIntegrationProfile.source_kind=="MOCK"`、seed `boot_id=="seed-history"`、明确测试 note。
- `scripts/reset_demo_data.py`：dry-run / `--export` JSON 备份 / `--execute`（仅 `app_env=="dev"` 或 `--force-server`）；审计日志永不删除。
- 文档 `docs/sim-data-cleanup.md`：生产建议全新库/新 site，老库不猜删。

## 自动检查

| 检查 | 结果 |
|---|---|
| `npm run typecheck` | ✅ PASS |
| `npm run lint` | ✅ PASS |
| `npm run test` | ✅ PASS（6 files / 20 tests） |
| `npm run build` | ✅ PASS |
| `python -m py_compile scripts/reset_demo_data.py` | ✅ PASS |

## 诚实声明：E2E / 截图未执行

与 Gate-2/3 相同，本沙箱无法运行真实 Chromium（Playwright CDN `400 GatewayExceptionResponse`、无 root 装 apt）也无法起后端栈（无 Docker）。因此 `npm run test:e2e` 与五视口/`/tasks`/`/alarms`/`/audit` 截图**没有生成，不标 PASS**。

`e2e/ui-geometry.spec.ts` 已更新为检查顶栏 4 个遥测格 + 设备快照卡，供本机一键验收：

```powershell
cd C:\Users\13576\Desktop\web_robot
.\scripts\dev.ps1
cd apps\web
npx playwright install chromium
npm run test:e2e
npm run test:e2e -- ui-geometry
```

## 业务真实性保留

- 火点/机器人坐标、扇区几何、视频 WHEP、急停 hold、权限、幂等、审计链路均未改动。
- 本轮后端仅 Gate-3 已提交的 create-task 状态放宽，未新增后端行为修改。
- 模拟数据清理脚本未在沙箱执行（需 Postgres + venv）。

## push

仍无法从沙箱 SSH 认证 GitHub，需你本机：

```powershell
cd C:\Users\13576\Desktop\web_robot
git push -u origin ui/youdao-light-hmi-v1
```
