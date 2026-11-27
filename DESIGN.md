# DESIGN.md — Firebot Web V2.1 工业操作工作台设计系统

> 适用：`feature/firebot-r002-web-v2-1`（基线 `23854b0`）。读者：前端/后端/车端研发与独立审查者。
> 本文是**锁定系统**：Web V2.1 所有界面共享本文件的令牌、密度、状态与交互规则。与既有 `docs/技术合同/*` 冲突时，以技术合同为准；本文只补界面决策。
> 方法参考 Nutlope/hallmark 的 app-screen 规则（`references/flow.md` App screens 段）：控制台是“在里面工作的屏幕”，营销页的 hero/节奏/字数预算一律不适用；Floor（令牌、对比度、焦点、状态、无捏造数字）全部保留。

## 1. 不更换的东西

保持 Vue 3 + TypeScript + Pinia + Vue Router + TDesign + ECharts + Vite/Vitest/Playwright；保持“友道智造”品牌与白蓝工业主题；保持现有 API 接线、权限名、路由与状态管理。本文只规定**视觉与交互**，不改协议与控制资格判定。

## 2. 令牌（唯一来源：`apps/web/src/styles/tokens.css`）

所有颜色/字体只能经 `--yd-*`/`--td-*` 令牌引用，禁止在组件里写死色值或 `font-family`。现有令牌已足够，V2.1 只**新增**（不覆盖）以下语义令牌，仍在 `:root` 声明：

| 令牌 | 建议值 | 用途 |
|---|---|---|
| `--yd-state-live` | `var(--yd-success)` | 数据 LIVE/CONNECTED |
| `--yd-state-stale` | `var(--yd-warning)` | STALE，必须显示“旧数据·时间” |
| `--yd-state-unknown` | `var(--yd-text-3)` | UNKNOWN/NOT_CONNECTED，数值显示 `--` 绝不显示 0 |
| `--yd-state-sim` | `#7a5af8` + 文字徽标 `SIM` | 模拟数据，环境隔离可见 |
| `--yd-safety` | `var(--yd-danger)` | 急停/保护停车等安全动作专属描边 |
| `--yd-control-height` | `36px`（命中区 44px） | 工业密度控件高度 |
| `--yd-touch-target` | `44px` | 最小可点击区 |
| `--yd-tabular` | `font-feature-settings:"tnum" 1` | 坐标/数值/电量一律等宽数字 |

Floor 落地（当前 `responsive.css` 仅有 `prefers-reduced-motion` 块，其余待补）：每个交互元素需 `:focus-visible`；禁止 `transition: all`；只动 `transform`/`opacity`；`html,body` 用 `overflow-x: clip`。

## 3. 布局（地图主导，操作稳定可见）

### 3.1 监控台 `/monitor`（V2.1 目标栅格）

```
┌───────────────────────── 状态栏（紧凑，1 行，始终可见）──────────────────────┐
│ 车辆选择 │ 环境徽标 REAL/SIM │ 链路 设备 任务 安全 四级状态 │ 电量 · 烟雾(单位) │ 时间 │
├──────────────────────────────────────────────┬──────────────────────────────┤
│                                              │ 辅助区（视频）               │
│           地图（主导，占 60%+）              │  RGB / 热成像 双路           │
│  实时导航地图 / 车位地图 切换                ├──────────────────────────────┤
│  X/Y/yaw + 数据时间 + 质量标签               │ 任务操作（固定可见）         │
│                                              │  巡检/暂停/继续/目标/返航    │
│                                              │ ┌──────────────────────────┐ │
│                                              │ │ 保护停车（常驻，不随页签）│ │
│                                              │ │ 软件急停（按住确认）      │ │
│                                              │ └──────────────────────────┘ │
└──────────────────────────────────────────────┴──────────────────────────────┘
```

关键决策（相对基线截图 `evidence/web0-baseline/`）：基线 1366×768 下“开始巡检/停止/返航”掉到折叠线以下、视频被压成窄条。V2.1 改为：右侧操作列独立滚动且**底部动作条 sticky**；保护停车与软件急停**固定区块**，不随“自动导航/手动控制/监测”页签切换而消失；地图最小高度 420px，视频辅助区最小高度 200px。

### 3.2 地图场景编辑器 `/maps/:mapId/edit`（资产树 + 画布 + 检查器）

三栏：左资产树（点位分组：巡检/消防作业/返航/等待/车位/路线/走廊）；中画布（MapCanvas 复用，世界坐标编辑，禁止像素坐标直接生成目标）；右属性检查器（选中对象的精确 X/Y/yaw、容差、编号、属性）。顶部为版本状态条（DRAFT/PUBLISHED）+ 撤销/重做 + 校验结果 + 发布按钮（发布≠运动授权）。

## 4. 状态语义（严格区分，禁止推断）

同时显示且互不替代的四个维度：
- **通信**：ONLINE / STALE / OFFLINE / UNKNOWN（链路）。
- **设备**：逐设备 OK/DEGRADED/FAULT/OFFLINE/UNKNOWN/NOT_INSTALLED + 观测时间。
- **任务**：CREATED→…→SUCCEEDED/FAILED/CANCELLED，加 WAITING_OBSTACLE/PAUSED。
- **安全**：急停锁存、保护停车、控制冻结（`control_frozen`）。

数值质量（每个读数）：`LIVE`（显示值+时间）/`STALE`（显示值+“旧”）/`UNKNOWN`（`--`）/`NOT_CONNECTED`（`--`）/`ERROR`（`--`）/`UNSUPPORTED`（如已禁用的独立温度，显示“未接入”）。**未知、过期、SIM 必须与真实区分；未知不得以 0 代替。**

烟雾：单位未验证前一律显示原始值 + “单位待验证”，**禁止套用 300ppm 阈值**；电量 50%、温度 80℃ 阈值仅在对应数据为 LIVE 且单位已验证时生效（独立温度在 G3-R2 已禁用，当前显示“未接入”）（见 R002_OPERATION_CONTRACT §5）。

## 5. 控制资格（前端只呈现，不推导）

所有动作按钮的可用性完全取自服务端 `OperationReadiness.actions[action]`（`allowed`/`reasons`/`required_permission`/`permitted_for_user`）。前端**禁止**自行计算控制资格。禁用时展示 `reasons` 的可读中文与所需权限。前端仅可叠加纯展示锁（如本地 busy），但**不得把服务端 denied 变成 enabled**。

保护停车入口与软件急停的安全可见性不受页签/折叠影响；软件急停用按住确认（沿用 `useHoldToConfirm`），文案说明“软件急停不替代物理急停”。

## 6. 密度与控件（工业）

控件高 36px、命中区 44px；表格优先于卡片；统计数字用等宽数字、附标签、无未测量的趋势箭头；主色仅用于主操作、当前选择与状态，次要中性色承担侧栏/工具栏；每张表/卡片只一个主按钮样式、一个圆角层级。八个状态（默认/hover/focus-visible/active/disabled/loading/error/selected）必须齐备。

## 7. 每个数据区域的四态

加载（骨架与将出现的形状一致）/ 空（说明该放什么 + 一个填充动作）/ 错误（坏了什么 + 怎么办）/ 满。只画“满”状态视为只做了四分之一屏幕。状态存于 URL（筛选/tab/分页），破坏性动作可撤销或二次确认。

## 8. 地图坐标纪律

世界坐标 `frame_id=map`，x/y 米，yaw 弧度，0=+X 逆时针为正（与 `docs/技术合同/地图坐标合同.md` 一致；MQTT 1.3 线缆字段名为 `theta`）。屏幕坐标只用于绘制，`screenToWorld` 必须严格逆变换。禁止用浏览器像素坐标直接构造导航目标。地图编辑器“当前位置示教”以车端上报位姿建点，不依赖人工点击换算。

## 9. 组件与复用

复用 `MapCanvas.vue`、`src/lib/map-adapter.ts`、`stores/monitor`、`components/monitor/*` 的有效实现；`ManualControl.vue` 当前未被挂载，V2.1 在“手动控制”页签接入并显示独占控制权持有者。新增编辑器状态独立于监控 store，不污染实时快照。

## 10. 验收基准

设计随附真实浏览器基线截图（1366×768 与 1920×1080，见 `docs/开发验收/web-v2.1/evidence/web0-baseline/`），以及验收矩阵 `R002_ACCEPTANCE_MATRIX`。任何布局改动必须给两档分辨率截图复核。禁止把演示停车场固定坐标用于真实 R002。
