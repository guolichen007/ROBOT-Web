# MQTT 协议（1.2 frozen legacy + 1.3 current）

本仓库 MQTT 协议双版本共存：

| 版本 | Schema | 定位 |
| --- | --- | --- |
| `1.2` | `packages/protocol-schemas/firebot-message-1.2.schema.json` | **frozen legacy**（向后兼容，生成模型以此为准） |
| `1.3` | `packages/protocol-schemas/firebot-message-1.3.schema.json` | **current vehicle bridge contract** |

- **服务器**：`services/protocol.py` 同时接受 `1.2` 与 `1.3`；未知 `schema_version` 显式 reject。
- **车端 Bridge**：上行一律 `1.3`；下行 command 接受 `1.2` 与 `1.3`。

Vehicle envelope 必须含 schema_version、message_id、type、vehicle_id、boot_id、timestamp、seq。seq 在 `boot_id + topic` 内单调递增（availability/LWT 除外，见下）；平台记录 server_received_at 与 clock_skew_ms。

Topic 为 `robot/{vehicle_id}/{availability|heartbeat|capabilities|location|status|sensor|alarm|task_status|command|command_ack}`。availability/capabilities QoS1 retain=true；location/sensor/heartbeat QoS0 retain=false；status/alarm/task_status/ACK QoS1 retain=false。

command 全部 retain=false。manual QoS0/TTL500ms；stop/e-stop/业务命令 QoS1。Vehicle 使用 boot_id，Platform 使用 target_boot_id。普通命令无 current boot session 时返回 `ROBOT_BOOT_SESSION_UNKNOWN`；software e-stop 可 target null，但必须等待 ACK。

ACK 只允许 accepted/rejected/unsupported，并携带稳定 reason_code。accepted 代表车端应用层完成状态/参数校验并接受执行，不代表任务完成；完成由 task_status 表示。

## 1.3 相对 1.2 的差异

- **sensor capability-driven**：`smoke / bottom_ir / top_ir_max` 至少存在一个；缺失字段不出现、不伪造 0。
- **status partial**：`mode / battery / estop_active / active_task_id` 均可缺失，只发真实字段（`{"battery":82.4}` 合法）。
- **command_ack reason_code** 增加 `BRIDGE_ADAPTER_NOT_CONNECTED`（协议合法但 ROS 未实现）。
- **availability/LWT**：不按 seq 单调、不按 message_id 长期去重（LWT payload 连接前固定）；同一 boot 多次断线/重连，每次 offline 都必须生效。
