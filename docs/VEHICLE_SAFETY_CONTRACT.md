# Vehicle Safety Contract

Status: V2 Baseline contractual interface. It is not a certification of the physical vehicle.

## Responsibility boundary

The cloud platform provides authenticated intent, leases, expiry metadata, command identity, audit, ACK tracking and conservative UI state. It cannot close the final motion-safety loop after network loss. The ROS2/vehicle team owns local motion arbitration, watchdogs, actuator interlocks, hardware emergency stop and functional-safety validation.

## Mandatory vehicle behavior

- Stop locally when a manual pulse is older than 500 ms or the active lease/control session stops receiving valid pulses.
- Never continue manual motion indefinitely after broker, network, ROS2 bridge or process failure.
- Reject expired commands and commands whose `vehicle_id`, active `boot_id`, map version, capability or local state is invalid.
- Treat `command_id` as an end-to-end idempotency key. A duplicate may repeat its result, but must not repeat a dangerous physical action.
- After reboot, create a new `boot_id`; do not replay or resume commands from the previous boot context.
- Latch an accepted `emergency_stop` locally. Only an explicit, authorized and locally valid `reset_estop` may clear it.
- `command_ack.status=accepted` means application-level validation completed and the command was accepted for execution. It never means “MQTT packet received”.
- A physical emergency-stop circuit overrides every software command and remains independent of the platform.
- Apply local maximum speed, acceleration, steering, proximity and actuator limits. Cloud values are requests, not safety limits.

## Platform behavior

- Manual pulses are QoS 0, non-retained, last-command-wins and never enter the durable outbox.
- `stop_motion` and software `emergency_stop` are QoS 1, non-retained and remain unconfirmed until a valid ACK.
- Stale/offline robots reject motion/task/reset commands. An offline emergency-stop attempt is shown as not delivered/unconfirmed.
- Software emergency stop invalidates the active manual lease and takes the safety fast path.
- Durable task commands use a PostgreSQL command row plus transactional outbox, retrying the same `command_id`.
- The UI distinguishes created, queued, published, accepted, executing, succeeded, failed and unconfirmed.

“Stop sent” or “e-stop published” is never proof that the vehicle has stopped. Physical acceptance must be confirmed by the real vehicle integration and site tests.
