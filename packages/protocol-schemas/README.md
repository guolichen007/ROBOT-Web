# 协议 Schema 单一事实源

`firebot-message-1.2.schema.json` 是车云 MQTT 协议的唯一 canonical definition。

- Robot Integration Contract：`1.2.0`
- MQTT `schema_version`：`1.2`
- Vehicle 消息使用 `boot_id`。
- Platform command 使用 `target_boot_id`，禁止平台伪造车辆 boot。
- 生成模型、MQTT ingress、Mock Robot、protocol tester 和 ROS2 handoff 都必须由此定义同步。

执行 `python scripts/generate_protocol_types.py` 更新漂移戳；CI 使用 `python scripts/check_protocol_drift.py` 阻止 Schema、生成模型和实现漂移。
