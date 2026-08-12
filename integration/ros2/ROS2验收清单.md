# ROS2 首次接入验收清单

- [ ] Gate 1 Broker/TLS/identity
- [ ] Gate 2 availability/heartbeat/capabilities
- [ ] Gate 3 location/map/version/checksum/x/y/theta
- [ ] Gate 4 status/sensor/time sync
- [ ] Gate 5 command_id/ACK/idempotency
- [ ] Gate 6 stop_motion（安全区域）
- [ ] Gate 7 低速 manual + 500ms TTL + 断网停止
- [ ] Gate 8 software e-stop latch/reset
- [ ] Gate 9 patrol/Nav
- [ ] Gate 10 fire alert
- [ ] Gate 11 extinguish task（不启用真实机构）
- [ ] Gate 12 最后才开放真实灭火执行机构

前一 Gate 未签字通过，下一 Gate 不启用。
