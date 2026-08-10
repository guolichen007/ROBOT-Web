# CODEX MASTER SPEC — 智能灭火机器人云控平台 V2 Baseline（FINAL）

> 本文件是用户在 2026-08-10 提供的 FINAL 规格的仓库内权威副本。此前 V1、V2、Addendum 和零散补充说明全部失效；冲突时以本文件和对应实施追踪记录为准。

## 1. 交付级别

RUNNABLE V2 BASELINE + PRODUCTION-ORIENTED ARCHITECTURE + SERVER-DEPLOYMENT-READY。

必须在 Windows 本机完成 Web、API、PostgreSQL、Redis、Mosquitto、MediaMTX、Worker 和 Mock Robot 的真实全栈闭环。第二台服务器只交付部署就绪包，不声称已经部署。

## 2. 项目边界

平台负责 Web、后端、数据库、MQTT、实时事件、媒体接入、权限审计、机器人/地图/报警/任务/命令/历史管理、Mock、测试和部署。

严禁实现 ROS2 node、SLAM、Nav2、传感器驱动、底盘、灭火执行机构、车端 watchdog 和真实车辆硬件安全回路。Web 与车端只通过 MQTT + Media Protocol 交互；浏览器不直连 ROS2、DDS、MQTT 或 RTSP。

## 3. P0 不变量

- 无有效 ACK/task status 时不得显示执行成功。
- 所有 command retain=false；QoS1 按 at-least-once 处理。
- `command_id`、`message_id`、`Idempotency-Key` 必须端到端幂等。
- manual pulse 每约 150 ms、TTL 500 ms、QoS0、last-command-wins，不进 outbox、不逐条审计、不重放。
- 一车同一时刻一个 manual lease；STALE/OFFLINE、登出、失焦、隐藏、关闭、e-stop 时失效。
- 松开控制必须停止 pulse、发送 QoS1 `stop_motion`、释放 lease。
- software `emergency_stop` 是最高软件优先级，但不等于物理急停；无 ACK 只能显示未确认。
- Durable business command 使用 PostgreSQL command + Transactional Outbox，同一 command_id 重试。
- boot_id 变化后旧实时控制语境失效；过期运动命令不补发。
- 机器人心跳 1 Hz，3 秒 STALE，10 秒 OFFLINE；LWT offline 可立即离线。
- Snapshot/Delta 使用可重放 watermark；gap 必须触发 resync。
- Published map version 不可原地修改；任务保存地图和目标快照。
- STALE/OFFLINE 不接受新的 manual、patrol、extinguish、return_dock、reset_estop。
- 所有正式时间使用 UTC/timestamptz，同时保留 source timestamp 和 server_received_at。
- server profile 禁止 Mock、匿名 MQTT 和 demo seed；refresh token 可轮换和撤销。

## 4. 固定技术与 Profiles

- Web：Vue 3、TypeScript、Vite、Pinia、Vue Router、Axios、Native WebSocket、SVG MapAdapter、Vitest、Playwright。
- Backend：Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL 18、Redis、Pytest、Ruff、mypy。
- MQTT：Mosquitto 2、MQTT 5 优先，协议兼容必要的 3.1.1 能力。
- Media：MediaMTX、WHEP、H.264 优先，预留 coturn。
- Proxy：Nginx。
- Profiles：`compose.dev.yml`、`compose.test.yml`、`docker-compose.server.yml`。

## 5. 协议合同

协议版本固定 `schema_version = "1.1"`。所有车端消息包含：

```json
{
  "schema_version": "1.1",
  "message_id": "uuid",
  "type": "location",
  "vehicle_id": "R001",
  "boot_id": "uuid",
  "timestamp": "UTC ISO-8601",
  "seq": 1
}
```

Topics：`location`、`status`、`sensor`、`alarm`、`task_status`、`command`、`command_ack`、`heartbeat`、`availability`、`capabilities`。

QoS：location/sensor/heartbeat/manual pulse 为 0；status/alarm/task_status/stop/e-stop/durable command/ACK/availability/capabilities 为 1。availability 与 capabilities retained，全部 command 非 retained。LWT 为 retained offline。

## 6. 必须完成的业务闭环

- 认证：Argon2id、access memory、refresh HttpOnly rotation/revoke、CSRF、登录限流、一次性 WS ticket。
- RBAC：六角色和细粒度 robot/alarm/map/user/audit/settings 权限。
- Realtime：boot_id/seq、clock skew、latest Redis、1 Hz PostgreSQL downsample、watermark/resync。
- Control：manual lease/session、manual pulse、stop_motion、software e-stop、Command/ACK、outbox、execution policy、离线策略。
- Map：Site、Map、MapVersion、semantic revision、车位、巡检点、灭火点、轨迹、secure asset upload、world/screen 坐标合同。
- Alarm：自动报警、人工火情、生命周期、dedup、A-12 地图派单。
- Task：patrol、extinguish、return_dock、cancel、冲突检查、快照、时间线。
- Mock：独立 MQTT R001，正常运行、任务、控制、e-stop latch、重启和故障注入。
- P2：MediaMTX/VideoProvider、metrics/logging、backup/restore、server compose、CI 和安全检查。

## 7. 验收与 Git 门禁

Backend、Frontend、Protocol、Container、Security、Playwright、故障核心项、真实浏览器闭环和 backup/restore smoke 全部通过后才允许 `develop -> main`。合并后在 main 重跑核心 smoke，推送 main 和 develop，最终工作区必须停留在干净 main。

最终报告严格包含 RESULT、RUN、URLS、GIT、SERVICES、DATABASE、MQTT、TESTS、ACCEPTANCE、KNOWN LIMITATIONS、ROS2 INTEGRATION CONTRACT、NEXT INPUTS 和 SERVER，并明确 `SERVER_DEPLOYED=NO`，除非确实部署到第二台服务器。

