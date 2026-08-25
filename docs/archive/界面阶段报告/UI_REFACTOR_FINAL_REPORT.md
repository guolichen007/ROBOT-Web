# ROBOT-Web 全站浅色工业 UI 重构 — 最终报告

基线分支 `hardening/pre-real-vehicle-v1` @ `5e98d37` → 工作分支 `ui/youdao-light-hmi-v1`

视觉 Source of Truth：`CLAUDE_UI_EXECUTION_CONTRACT.md`、`docs/02_VISUAL_GEOMETRY_SPEC.md`、`docs/03_DESIGN_SYSTEM.md`、`visual_prototypes/*.html` 与 `prototype.css`。未依赖参考 PNG 像素。

---

## 1. 修改文件清单

**样式（拆分自 2367 行的 main.css，全部新视觉走 `yd-` 命名空间）**

| 文件 | 说明 |
|---|---|
| `apps/web/src/styles/tokens.css` | 颜色/圆角/阴影/字号 token + TDesign 变量覆盖 |
| `apps/web/src/styles/base.css` | reset、typography、滚动条 |
| `apps/web/src/styles/shell.css` | Sidebar / Topbar / Page 框架 / PageHeader |
| `apps/web/src/styles/components.css` | Panel / Button / Form / Table / StateChip / Modal / 手动控制 / TDesign 浅色细化 |
| `apps/web/src/styles/auth.css` | 登录 + 改密 白蓝 split 布局 |
| `apps/web/src/styles/monitor.css` | Monitor 地图 / 右栏 / 底部 dock / 火警面板 |
| `apps/web/src/styles/pages.css` | History / Parking 页级布局 |
| `apps/web/src/styles/responsive.css` | 宽度 + 高度媒体查询（含 1366×768 压缩） |
| `apps/web/src/styles/main.css` | 仅 import 聚合 + keyframes |

**模板 / 组件**

| 文件 | 改动 |
|---|---|
| `apps/web/src/App.vue` | 去 compact sidebar；白蓝分组导航 + 二级子菜单；顶栏状态格绑定 monitor store 真实数据；品牌 logo + wave 资产 |
| `apps/web/src/views/LoginView.vue` | 友道 split 布局；记住账号只存 username |
| `apps/web/src/views/ChangePasswordView.vue` | 与登录同视觉，三个密码框 |
| `apps/web/src/views/MonitorView.vue` | 地图 + 右栏（视频/环境/巡检）+ 全宽底部 dock；火警态事件详情/处置移入右栏 |
| `apps/web/src/components/MapCanvas.vue` | 浅色主题、火点 marker（真实告警坐标）、动态图例、检测扇区蓝→红 |
| `apps/web/src/components/VideoCard.vue` | LIVE 徽标 + 时间戳覆盖层（WHEP 不动） |
| `apps/web/src/components/monitor/SituationBanner.vue` | 仅在火警时渲染顶部红色告警条 |
| `apps/web/src/components/monitor/RiskTelemetryRibbon.vue` | 改为右栏“环境与设备状态”卡片（真实 smoke/top_ir/bottom_ir/定位） |
| `apps/web/src/components/monitor/VideoSurveillancePanel.vue` | 增加“实时视频”标题头 |
| `apps/web/src/views/HistoryView.vue` | ECharts 轴/网格/系列/tooltip 全部迁浅色 |

**资源（新）**

`apps/web/src/assets/yd/` — `brand/youdao_brand_logo.png`、`decorative/tech_wave.svg`、`auth/`(scene + shield + 3 feature icons)、`map/`(fire_marker + robot fallback + robot ref)。

**未改动**：`apps/api/**`、`packages/**`、`services/**`、所有 stores / composables / lib 业务逻辑（`api.ts`、`operations.ts`、`map-adapter.ts`、`useHoldToConfirm.ts`、`usePrimaryAlarm.ts`、`stores/*`）。

---

## 2. 新增组件清单

未新增 Vue 组件 —— 复用现有组件体系，通过拆分 CSS + 模板重排达到目标结构，降低回归风险。新增内容为：8 个 CSS 模块 + `assets/yd/` 资源目录。

目标 `docs/06_COMPONENT_ARCHITECTURE.md` 中的职责分离已通过现有组件实现：

- `SituationBanner` → 火警告警条
- `RiskTelemetryRibbon` → 环境与设备状态卡
- `PrimaryAlarmPanel` + `AlarmLifecycleActions` + `ExtinguishActionCards` + `OperationTimeline` → 右栏当前事件处置
- `VideoSurveillancePanel` + `VideoCard` → 实时视频
- `OperationsCommandDock` → 底部 5 操作
- `MapCanvas` → 世界坐标地图（含火点 marker、动态扇区、图例）

---

## 3. 页面完成矩阵（15 Page Checklist）

### Shared
- [x] Design tokens（tokens.css + TDesign 变量覆盖）
- [x] Sidebar（白底、全宽、分组 + 子菜单、logo、wave）
- [x] Topbar（链路/在线/电量/任务/定位 + 用户 + 时钟）
- [x] TDesign light overrides（--td-* 变量 + 组件类细化）
- [x] Panel / Button / Form controls / DataTable / StateChip / Modal / Drawer
- [x] Empty / Error / Loading（empty-state / quiet-state / form-error / t-skeleton）
- [x] Responsive（5 viewport 宽度 + 高度媒体查询）

### Auth
- [x] /login
- [x] /change-password

### Operations
- [x] /monitor normal（地图 + 视频/环境/巡检 + 底部 dock）
- [x] /monitor alarm（火警 banner + 事件详情/处置 + 红色扇区/火点）
- [x] manual drawer（ManualControl 视觉浅色，逻辑未动）
- [x] stop operation state（停止确认进度条浅色）

### Management
- [x] /tasks · [x] /alarms · [x] /robots · [x] /maps · [x] /users · [x] /audit
（均为共享 PageHeader/DataTable/StateChip + 全局 panel/button/table 样式统一，未逐页重写）

### Specialized
- [x] /history（ECharts 浅色）· [x] /patrol · [x] /parking（复用 MapCanvas）· [x] /settings（服务网格 + 双栏）

### Viewports（CSS 已实现，见“测试结果”限制）
- [x] 2048×997 · [x] 1920×1080 · [x] 1672×941 · [x] 1440×900 · [x] 1366×768
（通过 minmax/clamp/dvh + 宽度与 max-height 媒体查询实现；未做浏览器截图验收，见 §5）

### Gates
- [x] typecheck · [x] lint · [x] unit tests · [x] build
- [~] E2E — NOT EXECUTED（沙箱无 Chromium，见 §5）
- [x] no unintended apps/api changes（0 个）
- [x] no fake production telemetry（未写死 92%/37.4℃/78/84/R001 等；环境卡用真实 smoke/top_ir/bottom_ir/定位，巡检卡用真实 Task.progress）
- [~] no critical controls clipped（CSS 保证底部 dock 始终在 grid 末行、height-driven 压缩；未经浏览器几何验收）

---

## 4. 测试结果

| 检查 | 命令 | 结果 |
|---|---|---|
| typecheck | `npm run typecheck` | PASS |
| lint | `npm run lint` | PASS（0 warning） |
| unit tests | `npm run test` | PASS — 5 files / 12 tests |
| build | `npm run build` | PASS — `✓ built` |
| E2E | `npm run test:e2e` | **NOT EXECUTED**（见 §5） |

---

## 5. 仍存在的视觉偏差 / 环境限制

1. **未做浏览器像素/几何验收**：本沙箱无法下载 Playwright Chromium（下载超时，宿主 178s 上限；且 `esbuild` 原生二进制首次安装中断损坏，已重装修复）。因此 `docs/10` 要求的 Playwright bounding-box 验收与 `screenshots/` 截图**未生成**。响应式目标由 CSS（`minmax`/`clamp`/`dvh` + `max-height: 860px` 压缩规则）实现，但需你在本机跑 `npm run dev` + 人工比对 `references/*.png` 与五个 viewport 最终确认。
2. **Sidebar 分组结构**：按 `docs/04` 实现为 7 个一级项 + 二级子菜单（任务管理→任务调度/巡检计划；设备管理→车辆/地图版本/地图与点位；系统设置→系统状态/用户权限），未创建无业务意义的 `/realtime`。
3. **巡检卡指标**：数据源没有“区域完成点/总点数”字段，按 `docs/05` 只显示真实 `Task.progress` 与任务编号，不伪造 78/84、93%。
4. **环境卡字段**：以真实 `smoke`/`top_ir`/`bottom_ir`/`localization_status` 命名（顶部热像/底部红外），未新增“环境温度/地面温度”伪字段；UNSUPPORTED 显示“当前车型不支持”，未接入显示“未接入”。
5. **品牌 Logo** 为 raster PNG（目标裁切），若项目方后续提供官方 SVG 可直接替换，无需改 Vue。

---

## 6. 仍存在的业务限制（非本轮范围，未动）

- 手动控制租约、150ms 脉冲、松键/失焦/隐藏自动 stop_motion、watchdog 提示——逻辑原样保留。
- 软件急停 0.8s hold-to-confirm、物理急停区分——原样保留。
- 停止巡检文案保持“停止巡检”（后端是 stop 而非可 resume 的 pause）。
- 权限 gating（`auth.can`）、must_change_password 强制改密、令牌轮换、WHEP 媒体鉴权——均未改动。
- 代码味道未清理：TasksView 内 `robot_id: 'R001'` 硬编码、HistoryView `robot_id: 'R001'` 等历史遗留，未在 UI 重构中顺带改（业务语义风险），单列技术债。

---

## 7. 每阶段 commit SHA

| 阶段 | Commit | SHA |
|---|---|---|
| Phase 1 设计系统 + Shell | `feat(web-ui): establish youdao light design system and app shell` | `bd9233d` |
| Phase 2 登录 + 改密 | `feat(web-ui): redesign login and password rotation views` | `2e310bd` |
| Phase 3+4 Monitor 正常/火警 | `feat(web-ui): migrate operations dashboard to light hmi` | `dbb0ee2` |
| Phase 6 分析页浅色 | `feat(web-ui): unify analytics chart to light theme` | `6dfaa14` |
| 收尾清理 | `refactor(web-ui): drop dead login-form selectors from component styles` | `729ea02` |

> Phase 5（Tasks/Alarms/Maps/Robots/Users/Audit）无需模板改动：这些页面已通过共享 PageHeader/DataTable/StateChip + Phase 1 的全局浅色样式自动统一，故无独立 commit。

**未 push**。分支 `ui/youdao-light-hmi-v1` 留在本地，等你人工验收参考图后再决定合并。
