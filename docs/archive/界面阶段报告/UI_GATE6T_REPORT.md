# ROBOT-Web Gate-6T 收口报告

分支 `ui/youdao-light-hmi-v1`，起点 `3050a2d`（Gate-6S 报告），以远端当前分支为源，无 hard reset。

## 新 HEAD

```
(见 git log)
```

## 提交（5 个）

| SHA | 内容 |
|---|---|
| `451e73d` | `refactor(platform): generalize stop operation across autonomous motion tasks` |
| `8801a38` | `fix(mock): terminate active mission and preserve cursor on software estop` |
| `9723da7` | `fix(navigation): route first cruise entry around A27 safe corridor` |
| `a08d6d9` | `refactor(web-ui): unify stop, paused and estop recovery actions` |
| `(test)` | `test(platform): align follower tests with state-aware watchdog signature` |

## 根因修复

### 停止泛化（P0-1/P0-2/P0-8）✅
- 新增 `POST /robots/{robot_id}/stop-operation`：取消任意 active motion task（PATROL/RETURN_DOCK/NAVIGATE_TO_PRESET/EXTINGUISH）；`/stop-patrol` 保留为兼容别名。
- 审计/原因码从 `STOP_PATROL` 改为 `OPERATOR_STOP`；前端第二个按钮改「停止」，RETURNING 时可用。

### 急停终止旧任务 + 保存 cursor（P0-3/P0-4）✅
- Mock `emergency_stop` 在清除 active task 前，先发布 `cancelled / ESTOP_INTERRUPTED` 的 terminal task_status，并带上当前 checkpoint/waypoint cursor 与 progress。
- 解除急停只解除锁存，不自动恢复；前端落到 PAUSED_SAFE，不再因旧 EXECUTING Task 重新判成 PATROLLING/RETURNING。

### ResumeContext 正式化（P0-5/P0-6）✅
- `Task.parameters_json.resume_state`：AVAILABLE / CONSUMED_BY_RESUME / CONSUMED_BY_RETURN。
- 巡检被取消且带 live_route_cursor → AVAILABLE；继续巡检时旧任务 → CONSUMED_BY_RESUME；返航 SUCCEEDED 后 → CONSUMED_BY_RETURN。
- 前端 `resumeTaskId` 只认 `resume_state === 'AVAILABLE'`，不再扫描任意 CANCELLED patrol。
- `return_dock.py` 删除创建时错误的 `resume_cleared=True`。
- `useVehicleOperationState` 用 `PAUSED_SAFE + resumeOptions` 取代 `STOPPED_RESUMABLE`，新增 `ERROR_STOP_UNCONFIRMED`。

### 180° 返航一次成功（P0-7）✅
- follower 改为返回 4 元组（含 heading_error）；watchdog 状态感知：ROTATE 按 heading error 下降判进展（8s 超时），DRIVE 按 distance 下降判进展（4s 超时）。
- 独立脚本验证：90° 转向返回 `ROTATE / linear=0`，对齐直行返回 `DRIVE / linear=2.8`。

### A27 入口不穿车位（P0-9/P0-10/P0-11）✅
- 入口改为 `(1.2,1.2) → (1.2,27.0) → (8.0,27.0) → A-27`，不再横穿 A27。
- 新增 `segment_intersects_slot()` 碰撞几何助手；独立脚本验证 0 个 TRANSIT/TURN 段与停车位相交。
- `scripts/sync_demo_navigation.py` 已用同一 route builder 重建轨迹（旧库需 `--apply` 重跑）。

## 验证

| 检查 | 结果 |
|---|---|
| Python `py_compile`（route_builder/follower/tasks/operations/mock/mqtt-ingress/tests） | ✅ PASS |
| 入口几何 + 碰撞（独立脚本） | ✅ 入口经北侧通道，0 段穿车位 |
| follower（独立脚本 + 单测更新） | ✅ ROTATE/DRIVE/ARRIVE 语义正确 |
| `npm run typecheck` / `lint` / `test`（27）/ `build` | ✅ 全部 PASS |

## 必须诚实声明：运行时未验证

Gate-6T PASS 要求「实际全栈 Mock 跑通：巡检停止/返航停止/急停解除继续/180°返航一次成功/A27 不穿车位/完整巡检回等待区」。本沙箱无 Docker/Chromium/pytest，以下**未执行**：

1. 全栈 Mock 生命周期 E2E（停止/返航/急停/恢复）——后端只做了 `py_compile` + 独立脚本验证。
2. 180° 返航的运行时验证——数学上确认了 3s 统一 watchdog 与 0.8 rad/s 转向冲突并改为状态感知，但未在真实 MQTT+motion_loop 复现「第一次转圈第二次才走」。
3. 五视口/按钮状态截图——无 Chromium。
4. pytest 运行（test_follower / test_demo_route 需 sqlalchemy/pytest）。

## 你本机验收

```powershell
cd C:\Users\13576\Desktop\web_robot
python scripts/sync_demo_navigation.py --apply   # api venv，重建 A27 入口轨迹
.\scripts\dev.ps1
cd apps\web
npx playwright install chromium
npm run test:e2e -- gate6-cruise
# api venv 内：
cd ..\api
pytest tests/test_follower.py tests/test_demo_route.py -q
```

重点肉眼确认：巡检中「停止」、返航中「停止」都可用；巡检急停→解除后按钮变「继续巡检/已停止/返回等待区」，不自动恢复；180° 返航一次点击成功；地图初始路线从 A27 北侧绕入、蓝色虚线不穿 A27 车位。

## 仍未验证的真实边界

- 实车 ROS1 下行控制仍未触碰。
- StopOperation 未新增 interrupted_task_type/reason 列（本轮用 task_id + 审计原因码表达，避免迁移风险），如后续需要结构化字段再补迁移。
