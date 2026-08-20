# ROBOT-Web Gate-6U 收口报告

分支 `ui/youdao-light-hmi-v1`，起点 `477c1bc`（上一轮 continue-patrol/return），以远端当前分支为源，无 hard reset。

## 新 HEAD

```
aa99afd refactor(web-ui): render continue patrol and continue return from operation context
```

## 提交（5 个）

| SHA | 内容 |
|---|---|
| `a2244db` | `fix(mock): persist route cursor across every patrol waypoint` |
| `bd794be` | `feat(platform): add authoritative interrupted operation context` |
| `1f4d510` | `fix(platform): require patrol resume before fresh restart` |
| `321aa09` | `feat(navigation): plan shortest safe return path on corridor graph` |
| `aa99afd` | `refactor(web-ui): render continue patrol and continue return from operation context` |

## 根因修复

### 巡检早期停止后错误变回"开始巡检"（P0-2/P0-3/P0-4）✅
- Mock `active_execution` 改为 **waypoint 级 cursor**：每个 WAITING/TRANSIT/TURN/INSPECTION 都更新 `last_completed_waypoint_index` + `target_waypoint_index`。旧代码只在 INSPECTION 到达时更新，所以"离开等待区还没到 A27 就停止"时 cursor 缺失。
- `cancel_task` / `emergency_stop` 发布带 cursor 的 terminal 状态；mqtt-ingress 存 `live_route_cursor`（含 target/last_completed）；PATROL CANCELLED 一律 `resume_state=AVAILABLE`（不再依赖 cursor 是否存在）。

### 服务端禁止错误重新开始（P0-6）✅
- `POST /tasks/patrol-plan` 硬保护：存在 AVAILABLE 巡检 + 不带 `resume_task_id` → `409 PATROL_RESUME_REQUIRED`；无 resume context + 机器人不在 REMOTE_WAITING → `409 PATROL_START_REQUIRES_WAITING_AREA`。
- 即使 Vue 再出 bug，后端也不允许从任意位置直线切回 REMOTE。

### 继续巡检用 target 而非 cursor+1（P0-5/P0-9）✅
- `build_resumed_patrol_task` 优先 `target_waypoint_index`，缺失时用服务端 `infer_route_cursor`（当前 pose 投影到最近巡航段）推 target；恢复路径 = current pose → target → 剩余 canonical waypoint，绝不回 REMOTE。

### 返航改成安全道路图最短路径（P0-11..P0-15）✅
- `build_return_waypoints` 用显式安全车道图（西侧外围 x=1.2、四条列车道 x=8/14/32/40、南 y=1.0 / 北 y=27.0 / 顶 y=28.7 三条横移）+ Dijkstra，替代原 if-else 南/北 heuristic 和原路倒序。
- 独立脚本验证（A27/A21/A36/A45/A54/顶部 A12 六个位置）：**全部 0 穿车位**，且路径明显比原路倒序短（如 A36 附近 36m vs 原 86m）。

### operation_context 权威化（P0-7/P0-8/P0-17/P0-18）✅
- monitor snapshot 新增 `operation_context`（state/kind/task_id/cursor/checkpoint/can_continue/can_return），由服务端从 active/interrupted 任务计算。
- 前端 `MonitorView` 删除三套 `.find()` 历史任务猜测，只读 operation_context；`useVehicleOperationState` 对任意 interrupted kind 返回 PAUSED_SAFE，修复"返航被停但无 patrol 断点时掉回 IDLE"。

## 验证

| 检查 | 结果 |
|---|---|
| Python `py_compile`（route_builder/tasks/system/mock/mqtt-ingress） | ✅ PASS |
| 协议 schema JSON | ✅ 有效 |
| 返航最短路径（独立脚本，6 个位置） | ✅ 全部 0 碰撞、路径更短 |
| 早停 resume（独立脚本） | ✅ infer target、续跑不回头 |
| `npm run typecheck` / `lint` / `test`（27）/ `build` | ✅ 全部 PASS |

## 必须诚实声明：运行时未验证

Gate-6U PASS 要求"实际全栈 Mock 跑通：开始→立即停→继续巡检不回起点；返回→停→继续返回→最近安全路线→等待区"。本沙箱无 Docker/Chromium/pytest，以下**未执行**：

1. 全栈 Mock 生命周期 E2E（早期停止 resume、返航最短路径、继续返回）——只做了 `py_compile` + 独立脚本几何验证。
2. 五视口/按钮状态/地图返航高亮截图——无 Chromium。
3. pytest 运行（需 sqlalchemy/pytest 与真实 DB）。

## 你本机验收

```powershell
cd C:\Users\13576\Desktop\web_robot
.\scripts\dev.ps1
cd apps\web
npx playwright install chromium
npm run test:e2e -- gate6-cruise
```

重点肉眼确认两条链：
1. 开始巡检 → 还没到 A27 就停止 → 按钮变"继续巡检" → 点它从当前位置继续去 A27，绝不回 REMOTE。
2. 巡检停止 → 返回等待区 → 返航途中停止 → 按钮变"继续返回" → 点它从当前位置走最短安全通道回 (1.2,1.2)，不穿车位、不原路绕圈。
以及：人为不传 resume_task_id 但库里有 AVAILABLE 巡检时，服务器返回 409，不会启动完整路线。

## 仍未验证的真实边界

- 实车 ROS1 下行控制仍未触碰。
- 返航路径地图高亮（巡检虚线 + 返航实线）本轮未做前端可视化，只做了路径规划，留待视觉验收时补充。
