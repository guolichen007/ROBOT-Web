# 真实机器人接入检查

- [ ] 使用 contract 1.2.0 / schema 1.2 canonical JSON Schema。
- [ ] 每次车端进程启动生成新 boot_id；seq 在 boot+topic 内单调递增。
- [ ] availability/LWT、heartbeat、capabilities Gate 通过后才发送业务数据。
- [ ] location 的 site/map/version/checksum/frame/x/y/theta 与 Published 地图一致。
- [ ] 相同 command_id 幂等；target_boot_id 不匹配拒绝。
- [ ] manual 500ms TTL 使用本地 monotonic receive time，断网自动停止。
- [ ] stop、software e-stop、latch/reset 在安全区域验证。
- [ ] ACK accepted 为应用层校验通过，任务完成由 task_status 提供。
- [ ] 现场参数只填 [ROS2 参数模板](../integration/ros2/ROS2_对接参数模板.yaml)。
- [ ] 按 [现场验收清单](../integration/ros2/ROS2_验收清单.md) Gate 顺序签字。
