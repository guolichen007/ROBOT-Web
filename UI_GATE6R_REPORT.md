# ROBOT-Web Gate-6R 收口报告

分支 `ui/youdao-light-hmi-v1`，起点 `d84a2ec`（Gate-6 报告），以本机工作区为源，无 hard reset、无 pull 覆盖。

## 新 HEAD

```
630b0ae fix(web-ui): normalize robot body frame, sprite, and coverage layering
```

## 提交（3 个）

| SHA | 内容 |
|---|---|
| `ec7df65` | `fix(mock): replace orbiting waypoint follower with turn-drive state machine` |
| `401f292` | `refactor(api): make cruise trajectory include all 54 inspection checkpoints` |
| `630b0ae` | `fix(web-ui): normalize robot body frame, sprite, and coverage layering` |

## 根因修复

### A28 自转（P0-1/P0-2）✅ 代码 + 纯函数验证
- 新增纯函数 `app/modules/navigation/follower.py`：`follower_command()` 状态机——heading error >8° 时 `linear=0` 原地转向，对齐后才 DRIVE（按距离调速 2.8/1.6/0.6），<0.25m ARRIVE 时 snap。
- 每 waypoint watchdog：3 秒无进展 → `PATH_FOLLOWING_STALLED` 失败，不再无限转圈。
- 独立脚本验证：90° 转向返回 `ROTATE / linear=0 / angular=0.8`；对齐直行返回 `DRIVE / linear=2.8`。
- 单测 `test_follower.py`：大 heading error 不前进、对齐按距离调速、ARRIVE 归零、heading 归一化。

### 54 点巡检（P0-3/P0-4）✅
- `build_cruise_waypoints()` 显式包含 54 个 INSPECTION waypoint（WAITING/TRANSIT/TURN/INSPECTION + slot_code + sequence），不再是“每列只两个端点”。
- 独立脚本验证：66 个 waypoint、54 个 INSPECTION、序列 1..54 连续、S 顺序正确、无斜切、起止 REMOTE_WAITING。
- Mock 同时消费 trajectory（运动）+ inspection 语义：到 INSPECTION 点 INSPECTING + dwell + checkpoint；`task_status` 新增 checkpoint_index/total/current_slot_code/next_slot_code（schema 扩了可选字段），mqtt-ingress 持久化到 `Task.parameters_json.live_checkpoint`。

### 检测范围实时跟车（P0-5）✅
- MapCanvas 改为用「服务端 sensor configuration + 最新 robot pose」实时投影 visualSector；不再等 1 秒一次的 polygon 轮询。服务端仍保留权威（covered ids / configuration / RIGHT 校验），前端只做显示投影。

### 车体坐标系 + 图片 yaw + R001 车尾（P0-6/P0-7/P0-8/P0-9）✅
- 新增 `lib/body-frame.ts`（+X 前 / +Y 左 / -Y 右，`vehicleRightVector=(sinθ,-cosθ)`、`bodyToWorld`）+ 四方向单测（6 个）。
- 机器人 sprite 用 `screenYaw = 90° - theta°` 补偿（素材车头朝图片顶部，`ROBOT_SPRITE_FORWARD_OFFSET_DEG=90`）。
- R001 标签移出旋转 group，放在车尾且保持水平。
- 传感器原点 glyph 用 `bodyToWorld(robot, mount_x, mount_y)` 定位在真实右侧安装点，扇区从这里发出。

### 检测图层 + 车位文字（P1-1/P1-2）✅
- 分层：floor → route → 极浅 underlay（α≈0.035，车位下）→ slot fill → 轮廓/3 条扫描线/原点（车位 fill 上、车位文字下）→ slot 文字 → fire → robot → robot 标签。
- 车位文字用 `textLength + lengthAdjust="spacingAndGlyphs"` + font 10px 自适应，避免小屏 A-01A-02 粘连。

### 巡检卡真实数据（P1-6）✅
- 巡检卡新增「巡检路线：右侧全覆盖 S 型 / 当前巡检 / 下一巡检 / 已巡检 N/54」，来自 `Task.parameters_json.live_checkpoint`，不写死。

## 验证

| 检查 | 结果 |
|---|---|
| Python `py_compile`（follower/route_builder/seed/sync/mock/mqtt-ingress/测试） | ✅ PASS |
| follower 纯函数（独立脚本 + 逻辑） | ✅ 90°→ROTATE、对齐→DRIVE |
| 轨迹构建器（独立脚本） | ✅ 54 检查点、顺序、无斜切、起止 REMOTE |
| `npm run typecheck` / `lint` / `test`（27）/ `build` | ✅ 全部 PASS |

## 必须诚实声明：未运行时验证的部分

按你的 PASS 标准，以下只能标 NOT EXECUTED（本沙箱无 Docker/Chromium/pytest）：

1. **完整全栈 Mock E2E**：A28 不再自转、54 点巡检、停止/急停中断、五视口截图——**未运行**。follower 状态机只做了纯函数 + 独立脚本验证，未在真实 MQTT + motion_loop 里跑。
2. **图片 yaw 基准 `90°`**：无法读取 `robot_topdown_v4.png` 像素，`ROBOT_SPRITE_FORWARD_OFFSET_DEG=90` 是依据“素材车头朝图片顶部”的推断，**必须你本机看四方向截图确认**（theta=0/π/2/π/-π/2 车头是否分别朝右/上/左/下）。
3. **pytest**：`test_follower.py` / `test_demo_route.py` 在沙箱无 sqlalchemy/pytest，只做了 py_compile。

## 你本机验收

```powershell
cd C:\Users\13576\Desktop\web_robot
# 旧库回填（全新库会自动 seed）
python scripts/sync_demo_navigation.py --apply     # api venv 内
.\scripts\dev.ps1
cd apps\web
npx playwright install chromium
npm run test:e2e -- gate6-cruise
npm run test:e2e -- software estop
# api venv 内：
cd ..\api
pytest tests/test_follower.py tests/test_demo_route.py -q
```

重点肉眼确认：A28 处先停、原地转向、再进 A29/A30；车头朝运行方向；R001 恒在车尾；扇区从右侧传感器原点发出并随车转动；扇区不遮车位文字；五视口顶部 A-01…A-18 不粘连。

## 仍未验证的真实边界

- 实车 ROS1 下行控制：本轮仍只到 Mock 层，未触碰。
- `MapAdapter.fitToBounds`（安全边距自适应）本轮未做——只修了车体/检测/跟随，缩放适配留到下一轮（已在本报告如实标注）。
