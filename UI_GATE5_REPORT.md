# ROBOT-Web UI Gate-5 收口报告

分支 `ui/youdao-light-hmi-v1`，起点 `fa7f229`（Gate-4 报告），无 hard reset、无 API 业务逻辑修改。

## 新 HEAD

```
9fb8700 refactor(web-ui): make top status bar responsive in two tiers
```

## 提交（2 个）

| SHA | 内容 |
|---|---|
| `ba40837` | `feat(web-ui): add latched estop reset workflow` |
| `9fb8700` | `refactor(web-ui): make top status bar responsive in two tiers` |

## P0-1 软件急停恢复闭环 ✅

- 复用后端已存在的 `POST /robots/{vehicle_id}/commands/reset-estop`（权限 `robot.control.reset_estop`），未新造接口。
- `MonitorView.resetEstop()`：POST reset-estop → `waitForEstopCleared()` 每 500ms 轮询 `GET /commands/{command_id}`（`ack_status === 'rejected'` → 被拒）与 `robot.estop_active === false`，最长 10s，超时区分「复位已下发但未收到状态确认」与「复位被车辆拒绝」。
- `OperationsCommandDock` 双态：`robot.estop_active` 为 false 显示红色「软件急停/按住 0.8 秒确认」；为 true 显示蓝色「解除急停/按住 0.8 秒复位」，二者均为 hold-to-confirm。
- 急停锁存时，开始巡检/停止巡检/返回等待区 `disabled`，Dock 上方提示「软件急停已生效，请先解除急停后再执行车辆运动操作」；解除成功后回到待命，不自动恢复原巡检任务。

## P0-2 急停态势 ✅

- `operationalSituation()` 新增 `ESTOP_ACTIVE`，优先级：严重火情 → 软件急停 → 离线未知 → 一般降级 → 正常。
- `SituationBanner` 对 ESTOP_ACTIVE 显示红底「软件急停已生效，车辆保持停止。确认现场安全后可解除急停」，不再显示裸 `DEGRADED`。
- 新增单测断言 `estopActive=true` 返回 `ESTOP_ACTIVE`。

## P1-1 顶栏响应式 ✅

- App.vue 将单一 `status-group` 拆为 `status-area > status-primary(5项) + status-telemetry-row(4项)`。
- ≥1600px 单行；<1600px 两行（第一行链路/机器人/电量/任务/定位 + 时间用户，第二行顶部热像/底部红外/烟雾浓度/数据更新，轻量无大图标）。
- 1366×768 下顶栏高度改为 88px（两行紧凑），不再出现文字重叠。

## P1-2 设备快照窄屏两列 ✅

- 删除 tight viewport 下 `.ds-grid { grid-template-columns: 1fr; }`，改为 `repeat(2, minmax(0, 1fr))`，8 项 4 行 × 2 列，取消不必要纵向滚动。

## P1-3 最终中文化 ✅

- `SituationBanner` 不再直接输出 raw state，走 `situationLabel()`。
- `dockReason` / `readinessText` 对 `readiness_reasons` 逐条经 `reasonCodeLabel()` 映射，未识别的统一显示「控制链路尚未就绪」，`ROBOT_* / *_NOT_READY` 不再裸出。

## 自动检查

| 检查 | 结果 |
|---|---|
| `npm run typecheck` | ✅ PASS |
| `npm run lint` | ✅ PASS |
| `npm run test` | ✅ PASS（6 files / 21 tests，含 ESTOP_ACTIVE 单测） |
| `npm run build` | ✅ PASS |

## E2E / 截图（未执行，如实声明）

与之前一致，本沙箱无 Chromium（Playwright CDN `400 GatewayExceptionResponse`、无 root 装 apt）、无后端栈（无 Docker），因此：

- `e2e/baseline.spec.ts` 新增 `software estop latches and reset-estop recovers to standby`（急停 → 变解除急停 → 三按钮锁定 → reset → 待命 → 开始巡检恢复），**未运行**。
- `e2e/ui-geometry.spec.ts` 新增 1280×800 视口 + topbar 两行 `bounding-box` 不重叠断言（`status-primary.right <= user-side.left`、两行不交叠），**未运行**。

本机一键验收：

```powershell
cd C:\Users\13576\Desktop\web_robot
.\scripts\dev.ps1
cd apps\web
npx playwright install chromium
npm run test:e2e -- software estop
npm run test:e2e -- ui-geometry
```

## 未改协议 / 保留项

- 后端 reset-estop、Mock Robot 复位、MQTT estop_active 回传、急停 0.8s hold、Idempotency-Key、权限、审计均未改动。
- 灭火三按钮仍保持 Gate-4「可点 + 后端中文拒绝反馈」，不与 Dock 锁存绑死。

## push

仍需你本机执行（沙箱无法 SSH 认证 GitHub）：

```powershell
cd C:\Users\13576\Desktop\web_robot
git push -u origin ui/youdao-light-hmi-v1
```
