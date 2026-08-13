# ROS1 上行 MQTT 字段字典

本字典描述首台 ROS1 Noetic 车辆在**只读阶段**向平台 Compatibility Adapter 发布的原生 JSON。它不是新的 Canonical Schema；平台对外合同仍为 `1.2.0 / schema 1.2`。

## 通用规则

| 项目 | 要求 |
| --- | --- |
| Topic | `robot/{external_id}/{suffix}`；`external_id` 必须与服务器配置或管理员绑定一致 |
| 编码 | UTF-8 JSON object |
| `ts` | 必填推荐；Unix 秒（可含小数）或 UTC ISO-8601。缺失时平台只能使用接收时间并降低诊断可信度 |
| Retain | 全部 `false` |
| Canonical 防循环 | payload 含 `schema_version` 时 Compatibility Adapter 必须忽略 |
| 控制 | 本接口只定义上行；不得发布 Firebot command/ACK |

## 字段

| Topic 后缀 | 字段 | 类型/单位 | 必填 | 来源与语义 |
| --- | --- | --- | --- | --- |
| `pose` | `x`, `y` | number / m | 是 | `/amcl_pose.pose.pose.position`，全局 `map` 坐标 |
| `pose` | `yaw` | number / rad | 是 | AMCL quaternion 转 yaw，CCW 为正 |
| `pose` | `frame_id` | string | 是 | 只能为 `map`；其它 frame 拒绝 |
| `pose` | `cov_xx`, `cov_yy`, `cov_yawyaw` | number | 推荐 | AMCL 协方差诊断，不在未实测前自定 GOOD/LOST 阈值 |
| `odom` | `vx`, `vy` | number / m/s | 是 | `/odom.twist.twist.linear.x/y`；麦克纳姆横移 `vy` 不得丢弃 |
| `odom` | `wz` | number / rad/s | 是 | `/odom.twist.twist.angular.z` |
| `status` | `control_mode` | integer | 是 | `1=MANUAL`，`3=ROS`；3 只映射业务 `IDLE`，不等于 PATROL |
| `battery` | `battery_percentage` | number / % | 是 | `/robot_status`；平台防御性限制到 0..100 |
| `battery` | `battery_voltage/current/temperature/capacity` | number | 否 | 原样保存为诊断；单位由现场消息定义再次确认 |
| `battery` | `battery_charge_state`, `battery_cycle_count` | number | 否 | 原样保存为诊断 |
| `heartbeat` | `uptime_sec` | number / s | 推荐 | 进程启动后的单调累计时间；在线判定仍以服务器接收时间为主 |
| `nav/status`, `nav/result` | `goal_id`, `status` | string | 推荐 | 仅诊断；无三方 ID 关联时不得完成 Firebot task |

平台根据 `odom` 计算 `planar_speed = hypot(vx, vy)`。因此 `vx=0, vy=0.25, wz=0` 明确表示车辆仍在横移，绝不能累计静止确认帧。

完整可机读样例见 [ROS1上行MQTT接口示例.json](ROS1上行MQTT接口示例.json)。
