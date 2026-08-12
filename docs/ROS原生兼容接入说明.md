# ROS 原生兼容接入说明

本文说明首台真实车辆在尚未实现完整车云协议时，如何以只读方式接入 ROBOT-Web。平台正式车云合同仍冻结为 **Robot Integration Contract 1.2.0 / schema 1.2**；兼容接入不会把平台业务升级伪装成协议升级，也不会开发或修改任何 ROS2 节点。

## 1. 数据路径与安全边界

```text
ROS 现场程序 → MQTT 原生主题 → ros-compat-adapter
                                 ↓
                        Canonical 1.2 / 内部兼容事件
                                 ↓
                  mqtt-ingress → Redis/PostgreSQL → Web
```

- Adapter 只订阅消息代理，不访问 Web API，不写数据库。
- 带 `schema_version` 的消息被认定为 Canonical，由 Adapter 忽略，防止 Canonical → Compat → Canonical 循环。
- 未确认外部设备 ID 只进入发现列表，绝不采用 first-seen-wins 静默绑定。
- 真实车 `command/ACK`、地图和能力合同全部验证前，平台强制只读；`patrol`、手动控制、返回等待区等动作返回 `CONTROL_CONTRACT_NOT_VERIFIED`。
- `stop_motion` 与 `emergency_stop` 仍可记录安全尝试，但未获得有效车端 ACK 时只能显示“未确认/未送达”。

## 2. 原生上行主题与字段

`{external_id}` 是现场设备标识，例如 `firerobot-01`。

| 主题 | 必填字段 | 可选字段 | 平台结果 |
|---|---|---|---|
| `robot/{external_id}/pose` | `x`, `y`, `yaw`（或 `theta`） | `timestamp/ts`, `localization_status` | Canonical `location` |
| `robot/{external_id}/odom` | 至少一个 `vx/linear_speed`, `wz/angular_speed` | `timestamp/ts` | 内部速度通道 |
| `robot/{external_id}/battery` | `percentage`（或 `battery`） | `timestamp/ts` | 电量通道 |
| `robot/{external_id}/status` | `control_mode_str`（或 `mode`） | `timestamp/ts` | 运行模式通道 |
| `robot/{external_id}/heartbeat` | 无 | `uptime_sec/uptime_seconds`, `timestamp/ts` | Canonical `heartbeat` |

时间可以是 UTC ISO-8601 或 Unix 秒。数值缺失不会自动填 `0`；未收到的烟雾、顶部红外、底部红外和急停状态分别显示 `NOT_CONNECTED`，车型明确不支持时显示 `UNSUPPORTED`。

示例：

```json
{"ts": 1786498200.12, "x": 39.0, "y": 12.5, "yaw": 1.5708, "localization_status": "OK"}
```

```json
{"ts": 1786498200.13, "vx": 0.18, "wz": 0.0}
```

## 3. 绑定流程

推荐服务器明确配置：

```env
ROS_COMPAT_MODE=true
ROS_COMPAT_EXPECTED_EXTERNAL_ID=firerobot-01
SINGLE_ROBOT_INTERNAL_ID=R001
ROS_COMPAT_STALE_SECONDS=8
ROS_COMPAT_OFFLINE_SECONDS=15
```

未知外部 ID 时：

1. 查看 `GET /api/v1/integration/ros-native/discoveries`。
2. 人工核对车辆后调用 `POST /api/v1/integration/ros-native/aliases`：

```json
{"robot_id": "R001", "external_id": "firerobot-01"}
```

3. `GET /api/v1/robots/R001/integration` 检查各数据通道质量。
4. 完成现场 ACK、地图版本和安全联调后，由管理员 `PUT /api/v1/robots/R001/integration` 逐项开启验证标志。

## 4. 数据来源与调试留存

每个通道记录：`source_kind`、`support_state`、`quality`、`last_source_timestamp`、`last_received_at`、`last_error`。原始兼容消息只在 Redis Stream `firebot:ros_compat:raw` 短期保存，限制单条 16 KiB、最多 1000 条、保留约 15 分钟，并屏蔽 token/password/secret 等字段；不会进入长期遥测事实表。

## 5. Phase A 验收

- [ ] 外部 ID 经过显式配置或管理员确认。
- [ ] 连续 15 分钟 pose/odom/battery/heartbeat 无结构错误。
- [ ] 坐标轴、单位、角度方向、`frame_id=map` 与发布地图一致。
- [ ] 位置和 Web 地图动态一致，右侧检测扇区始终位于车体右侧。
- [ ] 断流 8 秒显示 STALE，15 秒显示 OFFLINE，恢复后不会重放旧运动命令。
- [ ] 未提供传感器显示未接入而不是 `0`。
- [ ] Canonical 1.2 消息不会被 Adapter 二次处理。
- [ ] 控制验证前所有普通运动入口保持禁用。

正式 Canonical MQTT 字段、QoS、TTL、LWT 和 ACK 语义继续以 [MQTT协议.md](MQTT协议.md) 与 `integration/ros2/` 交付包为准。
