# API 与错误合同

REST 前缀 `/api/v1`。主要模块：`auth`、`robots`、`commands`、`sites/maps`、`tasks`、`alarms`、`history`、`users/roles/settings/audit`、`media`。

WebSocket：先以 access token 调 `POST /api/v1/auth/ws-ticket`，再连接 `/ws/v1/monitor?ticket=...&after=<watermark>`。ticket 一次性且短期有效，长期 JWT 不进入 URL。

媒体：`POST /api/v1/media/tickets` 需要 `robot.read`，返回短期 WHEP URL。MediaMTX 内部回调 `/api/v1/media/authorize`，外部用户不直接调用。

错误统一为：

```json
{"error":{"code":"ROBOT_OFFLINE","message":"机器人当前离线","request_id":"UUID","details":{}}}
```

固定控制错误码包括 ROBOT_DISABLED、ROBOT_STALE、ROBOT_OFFLINE、ROBOT_BOOT_SESSION_UNKNOWN、ROBOT_CAPABILITY_UNSUPPORTED、ROBOT_ESTOP_ACTIVE、MANUAL_LEASE_CONFLICT、MANUAL_LEASE_INVALID、ACTIVE_TASK_CONFLICT、MAP_VERSION_MISMATCH、COMMAND_EXPIRED、COMMAND_ACK_TIMEOUT、COMMAND_REJECTED、COMMAND_UNSUPPORTED、AUTH_REQUIRED、PERMISSION_DENIED、INVALID_PROTOCOL_MESSAGE、PROTOCOL_VERSION_UNSUPPORTED。
