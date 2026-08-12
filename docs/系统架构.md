# 系统架构

Vue 只连接统一 Nginx 入口；浏览器不直接连接 MQTT、RTSP 或数据库。FastAPI 是模块化单体，MQTT ingress、Command Dispatcher、Task Worker 是职责明确的独立进程。

PostgreSQL 保存用户、地图、任务、命令、报警、审计等业务事实；Redis 保存 latest、heartbeat TTL、manual lease、短期相关性和可重放实时 Stream。业务事实必须先 commit PostgreSQL，commit 后才发布 Redis delta。

Durable command 在同一事务写 `commands + outbox_events`，dispatcher 按 at-least-once 发布并保持相同 `command_id`。manual pulse 使用 Redis Pub/Sub、QoS0、TTL 500ms，不进入 outbox。stop/e-stop 使用独立高优先级 Stream，但数据库事实先提交。

Vehicle 消息使用 `boot_id`；Platform command 使用 `target_boot_id`。`robot_boot_sessions` 防止旧 boot 消息回滚当前会话。e-stop 可以使用 null target 进行离线安全尝试，但必须等待 ACK。

MediaMTX 的 read/publish 鉴权回调平台 `/api/v1/media/authorize`。Web ticket 绑定 user、robot、camera、stream 和 expiry；管理 API 只在容器网络或本机调试。
