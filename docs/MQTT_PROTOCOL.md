# MQTT 协议 1.2

Robot Integration Contract=`1.2.0`，`schema_version=1.2`。canonical Schema：`packages/protocol-schemas/firebot-message-1.2.schema.json`。

Vehicle envelope 必须含 schema_version、message_id、type、vehicle_id、boot_id、timestamp、seq。seq 在 `boot_id + topic` 内单调递增；平台记录 server_received_at 与 clock_skew_ms。

Topic 为 `robot/{vehicle_id}/{availability|heartbeat|capabilities|location|status|sensor|alarm|task_status|command|command_ack}`。availability/capabilities QoS1 retain=true；location/sensor/heartbeat QoS0 retain=false；status/alarm/task_status/ACK QoS1 retain=false。

command 全部 retain=false。manual QoS0/TTL500ms；stop/e-stop/业务命令 QoS1。Vehicle 使用 boot_id，Platform 使用 target_boot_id。普通命令无 current boot session 时返回 `ROBOT_BOOT_SESSION_UNKNOWN`；software e-stop 可 target null，但必须等待 ACK。

ACK 只允许 accepted/rejected/unsupported，并携带稳定 reason_code。accepted 代表车端应用层完成状态/参数校验并接受执行，不代表任务完成；完成由 task_status 表示。
