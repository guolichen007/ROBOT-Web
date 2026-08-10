# ROS2 Integration Contract

No ROS2 node is implemented in this repository. The future vehicle MQTT adapter connects outbound to the platform and is the sole bridge between ROS2/device state and the cloud protocol.

## Required adapter inputs

- Broker hostname/port; Internet deployments use MQTT TLS on 8883.
- Per-robot `vehicle_id`, credential and ACL.
- Protocol version 1.1, topics and QoS from `MQTT_PROTOCOL.md`.
- LWT retained offline payload and retained online/capabilities payloads.
- A new UUID `boot_id` on each adapter-process start and monotonic per-topic `seq`.
- UTC source timestamps backed by the site-approved NTP/chrony/PTP policy.
- Real map code/version/frame and capability list.

The R001 identity may publish only its location/status/sensor/alarm/task-status/ACK/heartbeat/availability/capabilities topics and subscribe only to `robot/R001/command`. It must not read or write R002. ROS2 DDS is never exposed to the browser or Internet.

The adapter enforces 500 ms manual TTL locally, command expiry, idempotent `command_id`, e-stop latch and explicit reset. It maps accepted/rejected/unsupported ACKs and task phases without weakening the semantics in `VEHICLE_SAFETY_CONTRACT.md`.
