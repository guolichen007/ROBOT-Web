# ROS1 Noetic 实车只读兼容接入说明

本文冻结首台实车在命令链路形成前的接入边界。平台车云合同仍为 **Robot Integration Contract 1.2.0 / schema 1.2**，本次只修改 Web/后端兼容层，不实现或修改任何 ROS1/ROS2 节点。

## 1. 已确认的静态接口

| 能力 | ROS1 真实来源 | 平台用途 | 当前状态 |
|---|---|---|---|
| 全局位置 | `/amcl_pose`，`PoseWithCovarianceStamped`，frame=`map` | Web 地图 `location` | 静态确认，待运行态采样 |
| 局部速度 | `/odom`，`nav_msgs/Odometry`，约 50 Hz | `linear_x/linear_y/angular_z/planar_speed` | 静态确认，待运行态采样 |
| 电池/底盘 | `/robot_status`，`igk_robot/RobotStatus`，约 10 Hz | 电量与诊断 | 静态确认，待运行态采样 |
| 导航 | `move_base` + TEB | 只保存诊断，不完成 Firebot 任务 | 下行未实现 |
| 底盘控制 | `/cmd_vel`，仅 `control_mode==3` 接受 | 本轮禁止发布 | 不具备测试条件 |
| 软件急停 | 无 topic/service/action | 显示 `UNSUPPORTED` | 不支持 |
| 灭火/视频/热像 | 当前代码库未发现 | 显示 `UNSUPPORTED` | 不支持 |
| 烟雾 | 独立 Modbus 脚本，非 ROS topic | 暂不接平台 | 未接入 |

真实底盘为麦克纳姆。静止判定必须使用：

```text
planar_speed = hypot(vx, vy)
stationary = planar_speed < linear_threshold
             AND abs(wz) < angular_threshold
```

`/odom` 的位姿永远不是 Firebot 全局位置；只有 `/amcl_pose` 可以更新地图位置。AMCL 协方差保存在兼容诊断中，首轮只以“消息新鲜且 frame=map”标记 `VALID_SOURCE`，不凭空制定质量阈值。

## 2. 只读数据路径

```text
ROS1 Noetic 现场 mqtt_bridge
  → robot/{external_id}/pose|odom|status|battery|heartbeat
  → ros-compat-adapter
  → _platform/compat/R001/*
  → mqtt-ingress
  → Redis/PostgreSQL/Web
```

- Adapter 不调用业务 API、不直接写遥测/任务事实；仅只读查询设备绑定与地图合同，归一化结果必须经 MQTT 进入 ingress。
- 带 `schema_version` 的 Canonical 消息由 Adapter 忽略，避免循环。
- 外部 ID 必须通过环境配置或管理员显式绑定，禁止 first-seen-wins。
- `nav/status` 与 `nav/result` 仅保存为 `robot_navigation_diagnostics`；没有 `task_id ↔ command_id ↔ ROS goal_id` 关联时绝不写 Firebot `task_status`。
- ROS_COMPAT capability 固定为空；缺失急停、烟雾、红外、视频均为 `UNSUPPORTED`，不得伪造 `0` 或 `false`。

## 3. 不可绕过的控制门禁

当前仓库不存在 ROS1 command publisher 与 ACK translator，因此 `ROS_COMPAT_DOWNLINK_IMPLEMENTED=false` 是代码级不变量。即使管理员把数据库 verified 标志全部置为 true，所有下行仍返回 `ROS_COMPAT_READ_ONLY`。

将来允许控制前必须同时完成：

```text
command/ACK bridge implemented and verified
control_mode == 3 runtime verified
cmd_vel arbitration verified
500 ms local monotonic watchdog verified
per-command capability advertised
map version/checksum matched (autonomous only)
```

真实车当前 `CMD_VEL_MUX=NONE`，不能让 move_base 与 Firebot 同时直发 `/cmd_vel`。未来车端结构必须至少为：

```text
move_base → /cmd_vel_nav ┐
Firebot   → /cmd_vel_fb  ├→ arbiter/twist_mux → /cmd_vel → igk_robot
safety stop              ┘

priority: SAFETY STOP > MANUAL > NAVIGATION
```

底盘 3000 ms watchdog 只可作为第二层兜底，不满足 Firebot manual TTL 500 ms。真正 command bridge 必须使用单调时钟实现 500 ms 本地 watchdog。本轮不执行 stop、manual、software e-stop、patrol 或灭火实车测试。

## 4. 上行 MQTT JSON

| Topic | 主要字段 | 处理 |
|---|---|---|
| `robot/{id}/pose` | `x,y,yaw,frame_id,cov_xx,cov_yy,cov_yawyaw,ts` | AMCL 全局位置 |
| `robot/{id}/odom` | `vx,vy,wz,ts` | 平面速度与静止安全 |
| `robot/{id}/status` | `control_mode,ts` | 单独保存 ROS control mode；1=MANUAL，3=ROS |
| `robot/{id}/battery` | `battery_percentage` 及 voltage/current/temperature/capacity 等 | 百分比映射 0..100，其余作为诊断 |
| `robot/{id}/heartbeat` | `uptime_sec,ts` | 在线/STALE/OFFLINE |
| `robot/{id}/nav/status` | `goal_id,status,ts` | 诊断，不关联任务 |
| `robot/{id}/nav/result` | `goal_id,status,ts` | 诊断，不关联任务 |

可直接交给 ROS1 mqtt_bridge 负责人的机读样例与逐字段合同见 [ROS1上行MQTT接口示例.json](../integration/ros1/ROS1上行MQTT接口示例.json) 和 [ROS1上行MQTT字段字典.md](../integration/ros1/ROS1上行MQTT字段字典.md)。

`control_mode=3` 是 ROS 控制器事实，绝不映射为 Firebot `PATROL`；业务 mode 保持 `IDLE`。公开 manual command 仍只有 `linear_x/angular_z`，本轮不向 schema 1.2 添加 `linear_y`，首次手动 Gate 不含横移。

## 5. 绑定与部署参数

```env
ROS_COMPAT_MODE=true
ROS_COMPAT_EXPECTED_EXTERNAL_ID=firerobot-01
SINGLE_ROBOT_INTERNAL_ID=R001
ROS_COMPAT_STALE_SECONDS=8
ROS_COMPAT_OFFLINE_SECONDS=15
```

未知 ID 先查看 `GET /api/v1/integration/ros-native/discoveries`，核对后调用 `POST /api/v1/integration/ros-native/aliases`。DEV 的 Mock 和 ROS_COMPAT 不应同时占用 R001 boot 会话；只读验收使用独立 profile/时段。

## 6. 地图合同

不修改 ROS `map_server`。现场对 `mymap.yaml + mymap.pgm` 生成确定性 SHA-256，并在 Server MapVersion 与 mqtt_bridge 同时配置：

```text
site_code
map_code
map_version
map_checksum
```

已知静态地图参数：resolution `0.05 m/px`，origin `[-7.126112, -11.984083, 0]`。版本号、checksum 和物理车头是否对应 URDF `+X` 仍由 OWNER 运行态确认。

## 7. 运行态只读验收

只允许 `rosnode list`、`rostopic list/info/echo/hz`、`tf_echo map base_link`；禁止 `rostopic pub`、service call、action goal 与任何车辆移动。验收清单见 [ROS1运行态只读验收清单.md](../integration/ros1/ROS1运行态只读验收清单.md)。

当前结论：

```text
ROS1_STATIC_INTERFACE_DISCOVERY=PASS
ROS1_RUNTIME_INTERFACE_VERIFICATION=REQUIRED
READY_FOR_ROS1_READONLY_AFTER_HARDENING=YES
READY_FOR_STOP_MOTION_TEST=NO
READY_FOR_MANUAL_MOTION_TEST=NO
READY_FOR_SOFTWARE_ESTOP_TEST=NO
READY_FOR_PATROL_TEST=NO
```
