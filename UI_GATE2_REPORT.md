# ROBOT-Web UI Gate-2 收口报告

分支 `ui/youdao-light-hmi-v1`，基线 `hardening/pre-real-vehicle-v1` @ `5e98d37`。

## 新 HEAD

```
812b030 test(web-ui): add viewport geometry and visual acceptance gates
```

## 提交

| # | SHA | 内容 |
|---|---|---|
| 1 | `f188059` | `fix(web-ui): restore situation warnings and update e2e selectors` |
| 2 | `c7f7542` | `refactor(web-ui): align monitor shell and command dock with target hmi` |
| 3 | `2f3f276` | `refactor(web-ui): compact alarm rail and map visual layers` |
| 4 | `812b030` | `test(web-ui): add viewport geometry and visual acceptance gates` |

## 修改文件

- Shell：`App.vue`、`styles/shell.css`、`styles/monitor.css`、`styles/responsive.css`
- Monitor 组件：`MapCanvas.vue`、`VideoCard.vue`、`monitor/{SituationBanner,RiskTelemetryRibbon,VideoSurveillancePanel,PrimaryAlarmPanel,ExtinguishActionCards,OperationsCommandDock}.vue`
- 视图：`MonitorView.vue`、`LoginView.vue`、`ChangePasswordView.vue`、`ParkingView.vue`
- E2E：`e2e/baseline.spec.ts`、`e2e/ui-geometry.spec.ts`（新增）
- **`apps/api/**`：0 改动**（`git diff --name-only 5e98d37..HEAD | grep -c apps/api` → 0）

## P1 安全项结果

| 项 | 结果 |
|---|---|
| P1-1 SituationBanner 恢复 | ✅ `NORMAL` 不显示；`alarm` 红色消防 Banner；`OFFLINE_UNKNOWN`/`DEGRADED`（含 estop 导致的降级）持久琥珀 Banner |
| P1-2 auth label for/id | ✅ `login-username/login-password`、`cp-current/cp-new/cp-confirm` |
| P1-3 E2E 选择器 | ✅ 同步为 `登录` / `停车场巡检地图` / `软件急停`；baseline 新增 normal 无 banner + 5 按钮断言 |
| P1-4 语义不造假 | ✅ `停止巡检` 保持 stop 语义；急停 hold 0.8s 保留；无假 LIVE/进度 |

## 本轮视觉修正（对照 Gate-2 的 10 项）

1. ✅ 火警/降级 Banner 提到 Topbar 之上（Teleport 到 `workspace-alert`）
2. ✅ 环境卡 4 个：top_ir / bottom_ir / smoke / 定位（freshness 移 header，视频状态回视频卡）
3. ✅ PrimaryAlarmPanel 双栏（详情 62~68% | 处置 32~38%）
4. ✅ “其他事件”默认折叠，约 56px，点箭头展开内部滚动
5. ✅ Dock 纯 5 按钮，reason 移细状态行 + disabled tooltip
6. ✅ MapCanvas 增加 layer prop：overview 默认隐藏语义点，`/parking` 保留
7. ✅ 真实俯视机器人图接进 marker（`worldToScreen(x,y)+rotate(theta)`，坐标不变）
8. ✅ 地图工具条 3 按钮（+/−/适配），header 右侧“图层”dropdown 可真实控制 route/coverage/semantic
9. ✅ Topbar 5 组图标状态 + 用户下拉（退出移入 dropdown，功能保留）
10. ✅ 巡检卡绑定真实 `Task.progress`，未造 93%/78/84/时长

## 数据诚实性

- 未写死 92%、37.4/32.0/3.75、78/84、93%、A-46、R001 坐标
- 火点/机器人坐标来自 `source_position_json` / slot 中心 / `robot.x/y/theta`
- 未把 top_ir/bottom_ir 冒名“环境/地面温度”
- 视频仍走 WHEP/RTCPeerConnection，未用参考 JPG 替代

## 自动检查

| 检查 | 结果 |
|---|---|
| `npm run typecheck` | ✅ PASS |
| `npm run lint` | ✅ PASS（0 warning） |
| `npm run test` | ✅ PASS（5 files / 12 tests） |
| `npm run build` | ✅ PASS |

## E2E / 浏览器几何 / 截图 —— 未在本沙箱执行

**结论：Gate D / E / F / G 未通过，不能标为完成。** 原因不是代码，而是本沙箱环境限制：

1. **无 Chromium**：`npx playwright install chromium` 与直连 Playwright CDN（`cdn.playwright.dev` / `playwright.download.prss.microsoft.com`）均失败——网关返回 `400 GatewayExceptionResponse`。`apt-get` 无 root 权限（sudo 被容器 `no-new-privileges` 拦截）。
2. **无后端栈**：E2E baseURL 是 `http://localhost:8080`（nginx→api+web），需要 Postgres/Redis/Mosquitto/MediaMTX/api/mock-robot，全部依赖 Docker，而沙箱无 `docker` 命令。

因此我已把验收写成可复现的 Playwright spec，并同步了 E2E 选择器，供你在本机一键执行：

```powershell
cd C:\Users\13576\Desktop\web_robot\apps\web
npx playwright install chromium
npm run test:e2e            # baseline + ui-geometry 全部
npm run test:e2e -- ui-geometry   # 只跑几何验收 + 截图
```

`e2e/ui-geometry.spec.ts` 会输出到 `apps/web/screenshots/ui-gate2/`：

```
1672x941-normal.png   1672x941-alarm.png
1366x768-normal.png   1366x768-alarm.png
2048x997-normal.png   1920x1080-normal.png   1440x900-normal.png
```

每张截图前断言 `devicePixelRatio === 1`（即 100% zoom），并逐项验证 Gate E 的几何断言（sidebar 185~230、topbar 64~92、dock 完整可见、环境卡 4 张、无横向滚动、alarm banner 在 topbar 上方、事件详情/处置左右并排、其他事件折叠 ≤72px）。

## 五视口验收状态

| 视口 | CSS 已实现 | 浏览器几何已验 |
|---|---|---|
| 2048×997 | ✅ | ❌ 待本机跑 |
| 1920×1080 | ✅ | ❌ 待本机跑 |
| 1672×941 | ✅ | ❌ 待本机跑 |
| 1440×900 | ✅ | ❌ 待本机跑 |
| 1366×768 | ✅ | ❌ 待本机跑 |

## 仍未与目标图一致的点（业务真实性原因，非时间原因）

1. **地图内容**：目标图里火点/机器人/停车位位置是那张特定地图的实时数据，本仓库用 `source_position_json`/slot 中心/`robot.x/y/theta` 动态计算，坐标不照图对齐——这是 Gate-2 明确要求的正确做法。
2. **环境卡字段名**：目标图写“环境温度/地面温度”，真实接口只有 `top_ir`/`bottom_ir`，故保留真实语义“顶部热像/底部红外”，不冒充温度。
3. **巡检卡**：目标图有 78/84、02:35:18 等，真实 `Task` 无 checkpoint 总数与持续时长字段，故只显示 `Task.progress` / `task_code` / `type`，不造数。
4. **图层 dropdown 是自绘**（TDesign Dropdown 无内建 checkbox 项），可真实切换 route/coverage/semantic，非装饰按钮。

## 你本机验收步骤

1. `.\scripts\dev.ps1` 起全栈（记下打印的 admin 密码）
2. `cd apps/web && npm install && npm run test:e2e -- ui-geometry`
3. 打开 `apps/web/screenshots/ui-gate2/` 的四张截图，与 `references/TARGET_*` 比对
4. 如有偏差，把截图发我，我按第二轮反证逐块收敛

**未 push**，等你本机验收后再决定。
