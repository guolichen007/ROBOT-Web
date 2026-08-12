# 变更日志

## v2.0.0-integration-ready

- 将 `docs/` 与 `integration/` 内全部文件名统一为中文，并同步更新生成器、测试、Manifest、文档链接与仓库策略引用。
- ROS2 对接 ZIP 内文件名同步中文化；协议字段、Topic、JSON/YAML/CSV 格式和 canonical Schema 内容保持兼容。
- 冻结 Robot Integration Contract 1.2.0 / MQTT Schema 1.2。
- 修复 PostgreSQL commit 与 Redis realtime event 顺序。
- 平台命令改用 target_boot_id，加入 boot session 防回滚。
- 加入 MediaMTX HTTP auth、短时 media ticket 和 H.264 测试源。
- SERVER health、preflight、最小公网暴露、Docker secrets 和安全响应头完成。
- telemetry/sensor 改为真实月分区与分区 retention。
- dispatcher consumer/client identity 唯一化。
- 统一 machine-readable API/ACK 错误码和 MQTT ingress 防护。
- 消除 History 大 chunk warning，统一 WS/lease/ACK 异常体验。
- 新增中文工程文档、GitHub 模板、CodeQL、Dependabot、ROS2 handoff ZIP/SHA256。
- Python 镜像加入慢网络 read timeout/重试策略，并在真实连接失败后验证恢复构建。
- ROS2 handoff 生成器固定 LF、ZIP 时间戳/权限/排序并归一化文本成员，确保跨 worktree SHA256 一致。
- CI 将 ROS2 对接 ZIP 与 SHA256 作为 `firebot-ros2-integration-1.2.0` artifact 上传，缺文件直接使门禁失败。

## V2 Baseline

- 建立可运行 Web/API/PostgreSQL/Redis/MQTT/MediaMTX/Mock Robot 基线。
