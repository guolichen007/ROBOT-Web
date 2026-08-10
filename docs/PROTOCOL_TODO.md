# Protocol TODO — Real-world Inputs

These items do not block Mock-based V2 Baseline and must not be guessed:

- Production vehicle IDs, models and per-robot credentials.
- Confirmed supported commands/capabilities and firmware/protocol compatibility matrix.
- Vehicle-local maximum linear/angular speed, acceleration and actuator limits.
- Exact smoke, bottom-IR and top-IR units, ranges, calibration and invalid-value semantics.
- Real site/map checksum, version, semantic revision, frame alignment and localization states.
- Camera RTSP/SRT sources, codec/profile, frame rate, resolution and MediaMTX path names.
- LAN/Internet broker addresses, firewall/NAT, MQTT TLS CA/client certificate policy and ACL rollout.
- STUN/TURN addresses and credentials if WebRTC requires relay.
- NTP/chrony/PTP design and acceptable clock-skew threshold.
- Hardware e-stop, vehicle watchdog and actuator safety validation evidence.

Real ROS2, Nav2, SLAM, sensor drivers, bottom controller, CAN/serial implementation and extinguishing actuator remain outside this repository.
