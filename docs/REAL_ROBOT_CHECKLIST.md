# Real Robot Integration Checklist

- [ ] Vehicle outbound network reaches broker and time source.
- [ ] Per-robot TLS credential and ACL verified; R001 cannot access R002.
- [ ] LWT offline and retained online/capabilities behavior verified.
- [ ] All protocol 1.1 messages validate against canonical JSON Schema.
- [ ] `boot_id` changes on restart and per-topic `seq` resets safely.
- [ ] Duplicate `command_id` does not repeat a dangerous action.
- [ ] Expired manual/task commands are rejected locally.
- [ ] Manual motion stops within the approved local watchdog interval after network loss.
- [ ] Software e-stop latches; explicit reset and hardware e-stop precedence verified.
- [ ] ACK accepted means local validation/acceptance, not MQTT receipt.
- [ ] Task phases and failure codes map to platform semantics.
- [ ] Map code/version/frame/checksum and coordinate transform validated at known points.
- [ ] Speed/acceleration/sensor units/capabilities entered from approved vehicle values.
- [ ] Camera provider paths and codec compatibility validated on target browsers.
- [ ] Site fault and physical safety tests signed off by the vehicle/site owner.
