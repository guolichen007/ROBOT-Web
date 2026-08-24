# 协议 Schema 单一事实源（1.2 frozen + 1.3 current）

本目录是 Firebot MQTT 协议的 canonical schema 来源，两个版本共存：

| 版本 | 文件 | 定位 |
| --- | --- | --- |
| `1.2` | `firebot-message-1.2.schema.json` | **frozen legacy**，仅用于向后兼容；生成模型（Python/TypeScript）以此为准 |
| `1.3` | `firebot-message-1.3.schema.json` | **current vehicle bridge contract** |

版本策略：

- 服务器 `services/protocol.py` 同时接受 `1.2` 与 `1.3`；未知 `schema_version` 显式 reject（不偷偷 fallback）。
- 车端 Bridge 上行一律 `1.3`；下行 command 接受 `1.2` 与 `1.3`。
- 1.2 保持 frozen，不再演进；1.3 的差异：sensor capability-driven（smoke / bottom_ir / top_ir_max 至少一个，缺失不伪造 0）、status partial、`command_ack.reason_code` 增加 `BRIDGE_ADAPTER_NOT_CONNECTED`。

生成模型：执行 `python scripts/generate_protocol_types.py` 更新 1.2 漂移戳；CI 使用 `python scripts/check_protocol_drift.py` 同时阻止 1.2 frozen 漂移与 1.3 current schema 失效。
