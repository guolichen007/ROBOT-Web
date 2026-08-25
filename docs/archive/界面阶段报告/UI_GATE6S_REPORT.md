# ROBOT-Web Gate-6S 收口报告

分支 `ui/youdao-light-hmi-v1`，起点 `cc9cf1a`（Gate-6R 报告），以本机工作区为源，无 hard reset。

## 新 HEAD

```
cd66877 refactor(web-ui): add telemetry health states and freshness status
```

## 提交（4 个）

| SHA | 内容 |
|---|---|
| `f69b3a7` | `fix(platform): reconcile stop patrol ack and stationary truth independently` |
| `58c6e5d` | `feat(platform): task-backed return-dock and route-cursor resume` |
| `056bd5f` | `refactor(web-ui): drive command dock and stop lifecycle from vehicle state` |
| `cd66877` | `refactor(web-ui): add telemetry health states and freshness status` |

## 根因修复

### 停止链 ACK/静止解耦（P0-1/P0-2）✅
- `task-worker.reconcile_stop_operations` 重写：物理静止（连续 5 帧零速）独立于 stop ACK 运行。ACK 丢失不再把操作冻结在 `UNCONFIRMED + stationary_frames=0`。
- 终态：`VEHICLE_STATIONARY_CONFIRMED`（全确认）/ `PARTIAL_UNCONFIRMED`（ACK 或 cancel 缺失但已静止）/ `UNCONFIRMED`（仍在动或遥测陈旧）。未确认物理静止时 UI 锁运动。

### 停止条生命周期（P0-3）✅
- `pollStop()` 终态后：成功 2s 清空、部分确认 3s 清空、无法确认保持可见并锁运动。
- 文案中文化，无裸 `CANCELLED_CONFIRMED/UNCONFIRMED` 枚举。

### 返航改成真实 Task（P0-4/P0-5）✅
- 新增 `POST /api/v1/tasks/return-dock`：创建 `Task.type=RETURN_DOCK` + 安全返航轨迹（`build_return_waypoints` 沿已验证巡航通道反向撤离，INSPECTION→TRANSIT，不穿车位）+ `cmd=return_dock` 带 `task_id`/`trajectory`。
- Mock 返航消费 `params.trajectory`，真正移动；Vue `home()` 改调该任务端点。

### route cursor + 继续巡检（P0-6/P0-7/P0-8）✅
- `task_status` 新增 `waypoint_index/waypoint_total`，mqtt-ingress 存 `live_route_cursor`。
- 中途停止后（存在 CANCELLED patrol + cursor），按钮变「继续巡检」，`patrol()` 传 `resume_task_id`；后端 `build_resumed_patrol_task` 用 `build_resumed_cruise_waypoints` 从当前 pose + 剩余 waypoint 续跑，不再回 REMOTE→A27。
- 返航成功（RETURN_DOCK 到等待区）后前端不再匹配到 CANCELLED patrol 的 cursor，自动回到「开始巡检」全新 54 点。

### 统一运行状态机 + Dock（P0-9/P1-1/P1-2）✅
- 新增 `useVehicleOperationState.ts`：IDLE/PATROL_STARTING/PATROLLING/STOPPING/STOPPED_RESUMABLE/RETURN_STARTING/RETURNING/ESTOPPED 等。
- `OperationsCommandDock` 按状态渲染「巡检中/停止中/返回中/继续巡检/已在等待区」，执行中按钮蓝色 active，其它按安全状态 disable；不再只靠 `busyCommand`（HTTP 请求态）。

### 数据实时四档 + 顶部状态颜色（P1-3/P1-4）✅
- `freshness` 从跳秒改成「数据实时 / 数据陈旧 Xs / 数据离线」；DeviceSnapshot 标题显示 `● 实时`。
- 新增 `telemetry-health.ts`，顶部链路/机器人/电量/任务/定位/热像/红外/烟雾/数据统一颜色语义，无硬编码安全阈值（温度/烟雾只按通道状态 + 告警着色）。

### 巡检卡 scalable 背景（P1-5/P1-6）✅
- 去掉 `card_bg_status_glow_*` PNG 整卡背景，改用 CSS 渐变（待命蓝/巡检绿/停止橙/返航蓝/急停红），任意宽度完整铺满；保留 `robot_state_*_art` 装饰。

## 验证

| 检查 | 结果 |
|---|---|
| Python `py_compile`（task-worker/mock/mqtt-ingress/tasks/route_builder） | ✅ PASS |
| 协议 schema JSON | ✅ 有效 |
| 返航/恢复路径（独立脚本） | ✅ 返航 12 点止于 REMOTE，恢复 55 点从当前 pose 续跑 |
| `npm run typecheck` / `lint` / `test`（27）/ `build` | ✅ 全部 PASS |

## 必须诚实声明：运行时未验证

PASS 标准要求「实际运行全栈 Mock：巡检→中途停→继续→再停→返航→再开始→完成 54 点」，本沙箱无 Docker/Chromium/pytest，以下**未执行**：

1. 全栈 Mock 生命周期 E2E（停止/继续/返航/重启）——后端只做了 `py_compile` + 独立脚本验证，未在真实 MQTT + 数据库跑。
2. 停止 ACK 丢失根因的运行时 trace——我做了「ACK 与静止解耦」这个结构性修复（无论 ACK 是否丢失都能继续物理静止验证），但未能在真实环境复现 UNCONFIRMED 的精确时序。
3. 五视口截图、按钮状态截图——无 Chromium。
4. `test_follower.py`/`test_demo_route.py` 的 pytest 运行——沙箱无 sqlalchemy/pytest。

## 你本机验收

```powershell
cd C:\Users\13576\Desktop\web_robot
.\scripts\dev.ps1
cd apps\web
npx playwright install chromium
npm run test:e2e -- gate6-cruise
npm run test:e2e -- software estop
# api venv 内：
cd ..\api
pytest tests/test_follower.py tests/test_demo_route.py -q
```

重点肉眼确认：巡检到 7/54 停 → 绿色「车辆已停止」约 2s 后消失 → 按钮变「继续巡检」（副提示从 A-20 继续）→ 点击后从 A20 续跑不回 REMOTE；再停 → 「返回等待区」→ Mock 真正沿通道回 (1.2,1.2) → 按钮「已在等待区」→ 「开始巡检」重新从 REMOTE 跑完整 54 点。

## 仍未验证的真实边界

- 实车 ROS1 下行控制仍未触碰；Mock 层演示闭环先跑通。
