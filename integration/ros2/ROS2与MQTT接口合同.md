# ROS2 MQTT 接口合同 1.2.0

- Vehicle 消息携带 `boot_id`；平台命令只携带 `target_boot_id`。
- 非 emergency-stop 命令没有当前 boot session 时必须拒绝。software e-stop 可以使用 `target_boot_id=null`，但 UI 必须等待 ACK。
- `command_id` 端到端幂等；所有 command `retain=false`。
- manual_control QoS0、TTL 500ms；stop/e-stop/业务命令 QoS1。
- ACK 仅允许 accepted/rejected/unsupported；accepted 表示车端应用层校验通过并接受执行。
- task_status 仅允许 accepted/executing/completed/failed/cancelled；未知 phase 必须安全透传。
- TTL 使用车端本地 monotonic receive time，不依赖源 UTC 时钟。
