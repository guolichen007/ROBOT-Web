# ROBOT-Web UI Gate-3 收口报告

分支 `ui/youdao-light-hmi-v1`，基线 `hardening/pre-real-vehicle-v1`。
本地 HEAD 与 `origin/ui/youdao-light-hmi-v1` 一致（`55555a0`），无 hard reset。

## 新 HEAD

```
2bc1cf6 feat(api): allow authorized extinguish dispatch before fire confirmation
```

## 提交（5 个）

| SHA | 内容 |
|---|---|
| `eaf0b8f` | `chore(web-ui): add gate-3 generated map and action assets` |
| `45f80ce` | `fix(web-ui): anchor detection overlay to vehicle right side` |
| `91c213c` | `refactor(web-ui): localize operator-facing states and fill video panel` |
| `b907e3a` | `refactor(web-ui): pause manual control and execute extinguish directly` |
| `2bc1cf6` | `feat(api): allow authorized extinguish dispatch before fire confirmation` |

## 七个硬目标落点

### A. 右侧检测永远在车辆右侧 ✅
- 新建 `lib/detection-geometry.ts`：`rightSensorProfile` / `buildSensorSector` / `isSectorOnVehicleRight`。
- 扇区由 `robot.x/y/theta` + RIGHT profile 的 `sensor_mount_*` / `coverage_fov_rad` / `coverage_range_m` 动态构造，随 theta 旋转。
- right half-plane guard：配置错误时**不镜像**、不画假扇区，隐藏并显示“右侧检测配置异常”。
- 新增单测：theta 0 / π/2 / π / -π/2 四向 + 反装无效，**20/20 通过**。

### B. 检测层不遮车位 ✅
- SVG 顺序改为：floor → coverage(soft-fill/dot/outline 三层) → route → slots → 语义点 → fire → robot。
- 透明度降到正常 ~4.5%、告警 ~6%，点阵 + 虚线轮廓，车位与 A-xx 文字永远在上层。

### C. 视频满高 ✅
- `VideoSurveillancePanel` 去掉 TDesign `t-tab-panel`，改为自有 tab bar + 单一 active `VideoCard`。
- CSS `grid-template-rows: 44px 42px minmax(0,1fr)`，video-stage 填满剩余高度；fullscreen 作用在容器。

### D. 手动控制停用 ✅
- Dock 从 5 按钮减为 4（开始巡检/停止巡检/返回等待区/软件急停）。
- MonitorView 不再 import/render `ManualControl`；`ManualControl.vue` 源码保留。
- E2E 增加 `expect(getByRole('button', { name: '手动控制' })).toHaveCount(0)`。

### E. 火情图标不遮车位 ✅
- 有 slot 的火情：slot 本身红框浅红底（A-xx 始终可读）+ 22~26px `fire_slot_badge_v4.svg` 放右上外侧 + leader line + 贴边自动翻左。
- 无 slot 只有坐标：`fire_pin_v4_64.png` 24~30px + 极浅 pulse ring，不再 40px 大圆。
- 机器人改用 `robot_topdown_v4.png`，仍 `translate(worldToScreen) + rotate(theta)`，vehicle_id pill 独立。

### F. 全量中文化 ✅
- 新建 `lib/ui-labels.ts` 集中映射；`StateChip`、环境卡、告警 Banner、事件详情、时间线、顶栏任务/定位、视频状态、History 三个 tab 全部中文。
- 保留 `FE-*`、`A-12`、`R001` 等编号；raw enum 不再进主视觉。
- 模板中已无 `{{ alarm.fire_type }}` / `{{ item.state }}` 等裸英文输出（已扫描确认）。

### G. 灭火一次点击直接执行 ✅（含最小后端放宽）
- 前端：`ExtinguishActionCards` 直接 `execute(mode)`，无“确认火情”前置、无“确认派发”；busy 防双击 + Idempotency-Key 保留。
- 后端：`alarms/router.py` create-task 前置从 `== CONFIRMED` 放宽为 `in {NEW, ACKNOWLEDGED, CONFIRMED}`；RESOLVED/已关闭仍 409。
- 权限 `extinguish.create`、幂等、审计、命令链路、readiness 冲突检查**全部未动**。
- 新增 API 测试 `test_extinguish_task_allowed_before_confirm_rejected_after_resolve`。

## 自动检查

| 检查 | 结果 |
|---|---|
| `npm run typecheck` | ✅ PASS |
| `npm run lint` | ✅ PASS |
| `npm run test` | ✅ PASS — 6 files / 20 tests（含 8 个新检测几何测试） |
| `npm run build` | ✅ PASS |
| `python -m py_compile`（后端改动文件） | ✅ PASS |

## 必须诚实说明：E2E / 截图 / API 测试未在本沙箱执行

- **Chromium 无法下载**：Playwright CDN 返回 `400 GatewayExceptionResponse`，无 root 装 apt（sudo 被容器 no-new-privileges 拦截）。
- **后端栈无法起**：E2E baseURL `localhost:8080` 依赖 Postgres/Redis/MQTT/API，本沙箱无 Docker。
- 因此 `npm run test:e2e`、五视口截图、以及上面新增的 API 集成测试（需真实 Postgres）**都未能运行**，我不会把它们标为 PASS。

新增的 E2E 断言与几何 spec 已写好，你本机一键执行：

```powershell
cd C:\Users\13576\Desktop\web_robot
.\scripts\dev.ps1            # 起全栈，记下 admin 密码
cd apps\web
npx playwright install chromium
npm run test:e2e             # baseline + ui-geometry
npm run test:e2e -- ui-geometry   # 只跑几何 + 截图
cd ..\api
# 在项目 venv 内运行 API 集成测试（需 postgres 已起）：
pytest tests/test_api_integration.py -k extinguish
```

截图输出到 `apps/web/screenshots/ui-gate2/`（1672/1366 的 normal+alarm，另含 2048/1920/1440 normal）。

## 业务真实性保留

- 火点/机器人坐标仍来自 `source_position_json` / slot 中心 / `robot.x/y/theta`，未照图对齐。
- `top_ir/bottom_ir` 未冒名“环境/地面温度”。
- 巡检卡只用真实 `Task.progress`，不造 78/84、02:35:18。
- 软件急停 0.8s hold、手动租约（API 层）、停止巡检确认、WHEP Bearer、权限 gating 均未弱化。

## 尚未 push

按你要求最终 push 到 `origin/ui/youdao-light-hmi-v1`，但本沙箱无法 SSH 认证 GitHub（`Permission denied (publickey)`），需你本机执行：

```powershell
cd C:\Users\13576\Desktop\web_robot
git push -u origin ui/youdao-light-hmi-v1
```
