# ROBOT-Web Gate-6 收口报告

分支 `ui/youdao-light-hmi-v1`，起点 `4784b2d`（Gate-5 报告）。以本机工作区为源，无 hard reset、无 pull 覆盖。

## 新 HEAD

```
9c2ad93 test(web-ui): add gate6 cruise and A-12 navigation e2e gates
```

## 提交（6 个）

| SHA | 内容 |
|---|---|
| `53ec4eb` | `feat(api): build right-side S-cruise route and 54 inspection presets` |
| `6d32828` | `feat(api): validate detection coverage right-side invariant` |
| `902a3fc` | `feat(mock): execute server trajectory with interpolated motion` |
| `54e1802` | `chore(platform): add idempotent demo navigation sync for legacy databases` |
| `084f17e` | `refactor(web-ui): consolidate coverage, alarm header, snapshot and labels` |
| `9c2ad93` | `test(web-ui): add gate6 cruise and A-12 navigation e2e gates` |

## 硬目标落点

### P0-1 车位导航预设点 ✅（代码 + 几何验证）
- 新建 `apps/api/app/modules/navigation/route_builder.py`，是 54 个 INSPECTION 预设点 + S 型轨迹的唯一权威构建器。
- seed 改为调用它（不再用旧的 InspectionPoint 位姿逻辑）；`scripts/sync_demo_navigation.py` 对旧库做幂等回填（绑定 `parking_slot_id` + 更新 pose + 重建轨迹/计划），且仅 `app_env=="dev"` 放行。
- 纯几何验证（本沙箱直接跑 Python）：54/54 车位在机器人右半平面、S 顺序正确、轨迹无斜切/越界、起止于 REMOTE_WAITING。

### P0-2 右侧检测巡检几何 ✅
- `inspection_pose()` 严格按五组实现：col1/col3 车位东侧 θ=-π/2，col2/col4 车位西侧 θ=+π/2，顶部车位南侧 θ=π。
- 全部满足 `dot(slot - robot, (sin θ, -cos θ)) > 0`（已实测 54/54 通过）。

### P0-3 完整巡航轨迹 ✅
- `build_cruise_trajectory()`：REMOTE_WAITING(1.2,1.2) → 西侧外围北上 → 第一列上方 → A27→A19 → 底部横移 → A28→A36 → 顶部横移 → A45→A37 → 底部横移 → A46→A54 → 顶部东端 A18 → A18→A01 → 西侧南下回 REMOTE_WAITING，全部 forward-only 轴对齐车道。
- 旧 `DEMO_LOOP` 不再作为完整路径；旧 `.limit(12)` 已删除，PatrolPlan 精确 54 个有序点。

### P0-4 检测范围唯一权威 ✅
- `calculate_detection_coverage()` 增加 RIGHT half-plane 校验，配置错误返回 `RIGHT_SENSOR_ORIENTATION_INVALID`，不镜像造假。
- 前端 MapCanvas 改为直接绘制后端 `coverage.polygon`，不再用 `detection-geometry.ts` 自算第二套几何（该文件保留供单测/离线预览）。
- 新增单测：54 个 preset 逐个 `calculate_detection_coverage` 必须 covered；反装 mount 必须返回 ERROR。

### P0-5 Mock Robot 真实执行轨迹 ✅（实现，未运行时验证）
- 删除 `self.patrol_route`，`simulate_task` 消费 `command["params"]["trajectory"]` 逐 waypoint 插值（加速车道 5 m/s、转弯 1.2 m/s，非瞬移），progress 单调 0→100。
- cancel/stop/estop 通过 `active_task_id` / `estop` 检查立即中断；完成后回 REMOTE_WAITING、mode=IDLE。
- **未运行时验证**：本沙箱无 Docker/MQTT，无法起全栈观察动画（见下方诚实声明）。

### P0-6 巡检计划选择 ✅
- `patrol()` 优先选 `RIGHT_SIDE_S_CRUISE_PLAN`，否则唯一 enabled plan，否则提示“请前往任务管理选择”。
- 地图 `trajectory` 优先取 `RIGHT_SIDE_S_CRUISE`，不再固定 `snapshot.trajectories[0]`。

### P1-1 小屏火情卡 ✅
- `AlarmLifecycleActions` 移出滚动区，进入 `PrimaryAlarmPanel` 固定头部；`确认收到/确认火情/标记解决` 始终可见；主体 = 详情(左) | 三灭火动作(右)，时间线在详情列底部。

### P1-2 设备快照 ✅
- 删掉与顶栏重复的“定位状态/数据更新”，主体 6 项（2 列 × 3 行），“数据 X 秒前”移到标题右上角；窄屏不转一列、无内部滚动条。

### P1-3 MapSelectionBar ✅
- `INSPECTION preset`→`巡检预设点`、`定位 未知`→`定位未知`，走 `localizationLabel`。

### P2 审计中文化 ✅
- 补齐 `NAVIGATE_PRESET_CREATE`、`ALARM_CONFIRMED`、`ALARM_ACKNOWLEDGED` 与 `resourceTypeLabel`（TASK→任务 / COMMAND→命令 / FIRE_EVENT→火情事件 / NAVIGATION_PRESET→导航预设点 / PATROL_PLAN→巡检计划）。

## 验证

| 检查 | 结果 |
|---|---|
| Python `py_compile`（route_builder / seed / operations / mock / sync / 测试） | ✅ PASS |
| 路由构建器几何（独立脚本） | ✅ 54/54 right-half-plane、S 顺序、无斜切、起止 REMOTE_WAITING |
| `npm run typecheck` / `lint` / `test`（21） / `build` | ✅ 全部 PASS |
| pytest（API 单测 `test_demo_route.py`） | ❌ 未执行（沙箱无 sqlalchemy/pytest） |
| 全栈 + Mock 动画 + Playwright E2E + 截图 | ❌ 未执行（沙箱无 Docker / Chromium） |

## 必须诚实声明：Gate-6 不能标 PASS

你明确要求“必须实际启动全栈和 Mock Robot，观察地图机器人沿新 S 路线移动后才能标 Gate-6 PASS”。本沙箱**无法**做到：

1. 无 Docker → Postgres/Redis/MQTT/MediaMTX/api/mock-robot 起不来；
2. 无 Chromium（Playwright CDN `400 GatewayExceptionResponse`、无 root 装 apt）→ E2E 与截图跑不了；
3. 无 sqlalchemy/pytest → 我写的 Python 单测（`apps/api/tests/test_demo_route.py`）在本环境无法运行（只做了 py_compile + 独立几何脚本验证）。

因此：**后端几何与轨迹我已用纯 Python 独立验证（54/54、顺序、连续性、覆盖），但 Mock Robot 沿轨迹运动的完整动画、A12 导航闭环、中途停止/急停中断，均停留在“代码已实现 + 语法/几何已验证”，未做运行时验证。** 我会在最终报告明确列出这条边界，不会伪装成“已打通”。

## 你本机验收命令

```powershell
cd C:\Users\13576\Desktop\web_robot
# 1) 旧库先回填（若库是全新的会自动 seed，无需这步）
python scripts/sync_demo_navigation.py --apply     # 在 api venv 内
# 2) 起全栈（mock robot + api + web）
.\scripts\dev.ps1
# 3) 前端 + E2E
cd apps\web
npm run test:e2e -- gate6-cruise
npm run test:e2e -- software estop
npx playwright install chromium   # 首次
# 4) API 单测（api venv 内）
cd ..\api
pytest tests/test_demo_route.py -q
```

预期画面：点“开始巡检”后，地图机器人从 (1.2,1.2) 出发，沿 S 路线 A27→A19→A28→A36→A45→A37→A46→A54→A18→A01，右侧检测扇区随 θ 实时旋转并始终朝向车位，最后回待命区、进度 100%。

## 仍未验证的真实边界

- **实车 ROS1 下行控制**：本轮只到 Mock Robot 模拟层，未触碰也不伪装打通 ROS1 真实控制链路，留作后续独立 Gate。
- `calculate_detection_coverage` 的运行时 `_polygons_intersect` 覆盖判定需在真实 Postgres + 全栈下用 pytest 最终确认（几何上已独立验证 54/54）。
