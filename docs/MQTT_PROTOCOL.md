# MQTT Protocol 1.1

JSON Schema under `packages/protocol-schemas` is the canonical definition. Generated Python and TypeScript artifacts must match its recorded SHA-256.

## Common envelope

Every robot message contains `schema_version=1.1`, UUID `message_id`, `type`, `vehicle_id`, per-process UUID `boot_id`, UTC ISO-8601 `timestamp` and monotonically increasing `seq`. The platform records `server_received_at` and `clock_skew_ms`. Ordering identity is `vehicle_id + boot_id + topic + seq`; event/command identity remains `message_id` or `command_id`.

## Topic policy

| Topic | QoS | Retain |
|---|---:|---:|
| `robot/{id}/location` | 0 | false |
| `robot/{id}/sensor` | 0 | false |
| `robot/{id}/heartbeat` | 0 | false |
| `robot/{id}/status` | 1 | false |
| `robot/{id}/alarm` | 1 | false |
| `robot/{id}/task_status` | 1 | false |
| `robot/{id}/command_ack` | 1 | false |
| `robot/{id}/command` manual pulse | 0 | false |
| `robot/{id}/command` stop/e-stop/task | 1 | false |
| `robot/{id}/availability` | 1 | true |
| `robot/{id}/capabilities` | 1 | true |

LWT is `availability=offline`, QoS 1, retained. On connect publish `availability=online`, QoS 1, retained. Every command topic is published with `retain=false`.

## ACK semantics

`command_ack.status` is `accepted`, `rejected` or `unsupported`. Accepted means local application validation completed and execution was accepted. Actual start/progress/completion comes from `task_status`. A missing or late ACK yields `PUBLISHED_UNCONFIRMED`; it never yields success. QoS 1 is at-least-once, not exactly-once.

Manual packets contain `lease_id`, `control_session_id`, `seq`, a 500 ms TTL and velocity request. They are not queued or replayed. Durable commands preserve one `command_id` across outbox retries.
