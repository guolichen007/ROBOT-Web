# CODEX MASTER SPEC — 智能灭火机器人云控平台 V2 Baseline（FINAL）

本文件是本轮开发的唯一最高优先级实施规格，合并并取代此前 V1、V2、Addendum 和零散补充说明。发生冲突时一律以本文件为准。

## 0. 本轮最终结论

交付层级固定为：

`RUNNABLE V2 BASELINE + PRODUCTION-ORIENTED ARCHITECTURE + SERVER-DEPLOYMENT-READY`

本轮必须在 Windows 本机真实运行 Web、API、PostgreSQL、Redis、Mosquitto、MediaMTX、Worker、Mock Robot，并完成浏览器闭环。它不是静态演示、P0 半成品、真实 ROS2 已接入、第二台服务器已部署或完整 Production Ready 认证。

## 1. 工作目录与 Git

- 正式远端：`git@github.com:guolichen007/ROBOT-Web.git`。
- Windows 目录：`C:\Users\13576\Desktop\web_robot`。
- 开始前检查目录、Git/SSH、Docker Engine 和远端是否为空；不得删除或覆盖非预期用户文件。
- 空仓库先建立并推送 `main`，再从 `main` 建立 `develop`；全部实现先进入 `develop`。
- 自动测试、全栈、E2E、故障、浏览器和备份恢复全部通过后才允许 `develop → main`。
- 合并后在 `main` 重跑核心 smoke，推送 `main` 和 `develop`，最终停留在干净 `main`。
- 禁止强推、提交 `.env`、私钥、证书、备份或真实密码/token，也不得删除失败测试来制造通过。

## 2. 项目边界

平台负责 Vue Web、FastAPI、PostgreSQL、Redis、MQTT Broker/ingress、Command Dispatcher、Task Worker、WebSocket、MediaMTX 接入框架、认证/RBAC/审计、机器人、地图版本、车位/点位/轨迹、报警、任务、命令/ACK、历史、Mock Robot、协议合同、测试、CI、备份恢复、可观测性和服务器部署包。

明确不开发或修改 ROS2 node、SLAM、Nav2、camera/infrared/smoke driver、fire detection algorithm、底盘、CAN/Modbus/串口、灭火执行机构、车端最终 watchdog 和硬件急停回路。Web/后台与现场团队只通过 `MQTT + Media Protocol` 交互，浏览器不得直连 ROS topic、DDS、`/cmd_vel`、`/tf`、Nav2 action、MQTT Broker 或 RTSP。

## 3. 车端安全责任合同

必须维护 `docs/VEHICLE_SAFETY_CONTRACT.md`。云平台不能承担网络失联后的最终运动安全闭环。未来真车必须保证 manual TTL 到期本地停车、断网不持续运动、过期命令不执行、相同 `command_id` 幂等、重启后旧命令不重放、软件急停接受后锁存、显式 reset、ACK accepted 代表应用层完成本地校验、硬件急停优先。平台显示 stop/e-stop 已发送不等于车辆已经停止。Mock 仅模拟这些合同，不代表真实 ROS2 已完成。

## 4. P0 系统不变量

- 未得到有效 ACK/task status 前不得显示执行成功。
- 过期 manual/control 不执行、不重放；所有 command topic 永远 `retain=false`。
- 同一机器人同时最多一个 manual lease；软件急停优先于普通任务和 manual。
- STALE/OFFLINE 不接受新的移动、巡检、灭火、回充或 reset-estop。
- `boot_id` 改变后旧实时控制失效；API/Worker/MQTT/Redis 重启不得重复非幂等动作。
- MQTT QoS1 按 at-least-once 设计，业务依赖 `command_id` 端到端幂等。
- WebSocket delta gap 必须 resync；Redis Streams 或等价日志保证 snapshot/delta 不静默丢事件。
- 所有语义对象绑定地图版本；PUBLISHED/ACTIVE 地图不可原地覆盖，任务固化目标/地图/语义/轨迹快照。
- 正式时间使用 UTC/timestamptz；在线判断以 server receive/monotonic 为主，不能只信源时间。
- SERVER 禁止 Mock、匿名 MQTT、示例账号/数据；refresh token 可轮换和撤销。
- 关键控制必须记录 operator、robot、command_id、target、time、result。
- migration 必须验证空库与可回退/升级路径；浏览器不直连 MQTT/RTSP；视频不通过 MQTT Base64。
- 150 ms pulse 不逐条写完整业务命令/审计；worker 不盲目重发 manual、过期或非幂等命令。

## 5. 固定技术栈

- Web：Vue 3、TypeScript、Vite、Pinia、Vue Router、Axios、Native WebSocket、SVG MapAdapter（必要时 Konva）、ECharts 仅用于趋势、Vitest、Playwright。
- Backend：Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、PostgreSQL 18、Redis、Pytest、Ruff、mypy。
- MQTT：Eclipse Mosquitto 2、MQTT 5 优先，兼容必要的 3.1.1 能力，不承诺 exactly-once。
- 视频：MediaMTX、浏览器 WHEP/WebRTC、H.264 优先，预留 STUN/TURN/coturn。
- 统一反代 Nginx；全部镜像固定 tag，禁止 `latest`。

## 6. 运行 Profiles

- `compose.dev.yml`：Mock ON、幂等 demo seed、开发工具、可配置匿名 MQTT、bootstrap admin。
- `compose.test.yml`：独立 PostgreSQL/Redis namespace、Mock 可控、故障注入、不污染 DEV。
- `docker-compose.server.yml`：Mock OFF、anonymous OFF、demo OFF、强 secret、TLS/ACL 挂载、restart、持久卷、日志轮转、备份模板、严格 CORS/origin、secure cookie、反代。
- SERVER profile 不得用 “Production Ready” 命名误导实际状态。

## 7. 仓库结构

仓库包含 `apps/web`、`apps/api`、`services/{mqtt-ingress,command-dispatcher,task-worker,mock-robot,protocol-tester}`、`packages/{protocol-schemas,generated-python,generated-typescript,shared-fixtures}`、`infra/{mosquitto,mediamtx,nginx,coturn,docker}`、`docs`、`scripts` 和三个 Compose profile。API 为模块化单体，仅把有明确进程职责的 MQTT ingress、dispatcher、worker 等拆开。

## 8. PostgreSQL 数据模型

- Site/Map：`sites`、`maps`、`map_versions`；版本含状态、checksum、semantic revision、尺寸、原点、旋转、分辨率、asset、frame、创建/发布时间。
- Semantic Map：`parking_slots`、`inspection_points`、`extinguish_points`、`trajectories`，全部绑定 `map_version_id`，数据库保存 world coordinates。
- Robot：`robots`、`robot_credentials`（只存安全元数据/引用）、`robot_capabilities`、`robot_connection_logs`。
- Auth：`users`、`roles`、`permissions`、关联表、`refresh_sessions`。
- Manual：`manual_control_sessions` 保存 HELD/RELEASED/EXPIRED/FORCE_RELEASED 摘要；实时互斥在 Redis 原子 TTL key。
- Task：`tasks` 保存状态/阶段/进度、目标、地图和轨迹快照；`task_events` 保存时间线。
- Command：`commands` 保存 command/correlation/task/priority/payload/lifecycle/ACK/时间；`outbox_events` 支持 durable command。
- Alarm：`fire_events` 支持 AUTO/MANUAL、fingerprint、计数、完整生命周期、位置/传感器/媒体快照。
- Telemetry：`telemetry_samples` 与 `sensor_samples` 为时间分区表，保留 source timestamp 与 server received；latest 进 Redis，PostgreSQL 默认 1 Hz downsample。
- 其他：`audit_logs`、`media_records`、`assets`、`system_events`、`app_settings`、stream registry。所有正式时间字段为 timestamptz。

## 9. Redis 边界

Redis 用于 latest state、heartbeat TTL、online/stale、manual lease、rate limit、短期 command correlation、实时事件、WS fan-out 和可重放短期 Streams。PostgreSQL 仍是 Task、Command、Fire Event、Audit、User、Map 等业务事实的最终事实源。

## 10. MQTT Protocol 单一事实源

协议版本固定 `schema_version="1.1"`，不因平台名 V2 擅自升为 2.0。所有车端消息包含 `schema_version`、`message_id`、`type`、`vehicle_id`、`boot_id`、UTC `timestamp`、`seq`；服务器补充 `server_received_at`、`clock_skew_ms`。

Topics：`robot/{id}/{location,status,sensor,alarm,task_status,command,command_ack,heartbeat,availability,capabilities}`。

- location/sensor/heartbeat：QoS0、retain false。
- status/alarm/task_status/command_ack：QoS1、retain false。
- manual pulse：QoS0；stop/e-stop/task command：QoS1；所有 command retain false。
- availability/capabilities：QoS1、retain true；LWT 为 retained offline，连接后发布 retained online。
- capabilities 声明 supported_commands、sensors、media，Web 必须据此禁用不支持功能。

## 11. Command 分类与可靠性

- Realtime manual：last-command-wins、约 150 ms、TTL 500 ms、QoS0、不进 outbox、不积压、不重放；携带 lease/session/seq。松键/失焦/隐藏停止 pulse，发一次 QoS1 `stop_motion` 并释放 lease。
- Safety stop：高优先级 QoS1，ACK 前只能显示已发送/待确认。
- Software e-stop：最高软件优先级、快速通道、立即失效 lease、阻止普通运动；publish 后等待 ACK，超时显示未确认，不能误报已停车。
- Durable commands：patrol、extinguish、return_dock、cancel_task 在同一事务写 Command + Outbox；dispatcher 重试保持 command_id，车端同 id 不重复危险动作。

## 12. Command/ACK 状态语义

状态至少含 CREATED、VALIDATED、QUEUED、PUBLISHED、ACK_ACCEPTED、EXECUTING、SUCCEEDED，以及 VALIDATION_REJECTED、ACK_REJECTED、ACK_UNSUPPORTED、PUBLISHED_UNCONFIRMED、EXPIRED、FAILED、CANCELLED。`accepted` 必须代表车端应用层完成参数/状态校验并接受，不只是 MQTT 收包；真正开始/完成由 task_status 表达。

## 13. Command Schema

Command 含 schema/message/vehicle/command/correlation/task、issued/expires/ttl/priority/source/operator/cmd/params。JSON command 统一 snake_case；`stop_motion` 与 `cancel_task` 语义分离。

## 14. Manual Control Lease

接口：`POST/DELETE /api/v1/robots/{id}/manual-lease` 与管理员 `POST .../force-release`。一车一 lease，Redis 原子 TTL；pulse 必须携带有效 lease。WS 断开、关闭、hidden、logout、权限失效、机器人 STALE/OFFLINE、e-stop 时释放。manual 与 active autonomous 默认互斥，不能自动静默抢占。

## 15. Robot Execution Policy

e-stop 打断软件运动；estop active 时禁止 patrol/extinguish/return_dock/manual；extinguish 高于 patrol 但必须显式终止/转换；manual 与 autonomous 互斥；return_dock 与 active extinguish 冲突；已有 active business task 时拒绝 patrol。使用 DB transaction + runtime state 即可。

## 16. Online/Offline/Time

车端 heartbeat 1 Hz；3 秒无心跳 STALE，10 秒 OFFLINE，LWT 可立即 OFFLINE。主要使用 server receive/monotonic 判断；记录 source timestamp、server received 和 clock skew。STALE/OFFLINE 拒绝 manual、patrol、extinguish、return_dock、reset；stop 可记录尝试但不得表示已停止；e-stop 可尝试但只能 NOT_DELIVERED/OFFLINE/UNCONFIRMED。重连不自动重放过期运动命令。

## 17. Map Version/Coordinate Contract

维护 `MAP_COORDINATE_CONTRACT.md`：`frame_id=map`、x/y 米、theta 弧度、零轴/正方向、world origin、rotation、resolution、pixel origin、screen y flip、world/screen 互换、checksum、version、semantic revision。数据库保存 world coordinates，MapAdapter 最后转换。任务派发前校验机器人地图与 published 目标一致，创建任务固化全部快照。

## 18. Snapshot + WebSocket Delta

REST snapshot 返回 `snapshot_watermark`；WS 使用 `after=watermark`，服务端重放后进入 live；事件带 stream id/seq。gap 或 replay window 过期发送 `resync_required`，客户端重新拉 snapshot。自动测试必须证明 snapshot 建立期间的 location/alarm/task 不静默丢失。

## 19. WebSocket Auth

Access token 只放前端内存；refresh token 使用 HttpOnly、SERVER Secure、SameSite、rotation/family revoke。客户端以 access token 请求 30–60 秒一次性 WS ticket，连接 `/ws/v1/monitor?ticket=...&after=...` 后 ticket 失效。禁止长期 JWT 放 URL，校验 Origin/auth/permission/reconnect。

## 20. Authentication/RBAC

使用 Argon2id、登录限流和临时锁定、CSRF、WS origin。角色为 super_admin、administrator、dispatcher、operator、viewer、auditor；权限细分 robot read/manual/stop/estop/reset/force-release/task、patrol、extinguish、alarm、map、user/role、audit、settings。DEV bootstrap 密码首次生成只显示一次并强制修改；SERVER 由 env/secret file 注入且不打印日志。

## 21. Fire Event/Manual Alarm

支持车端自动报警和 `POST /api/v1/alarms/manual`；人工事件参数含 parking slot、fire type、note、可选 media、map version，operator 只取 auth context。地图 A-12 支持创建→确认→灭火任务。按 message_id、event_id、fingerprint+时间窗去重，重复只更新 last_seen/count。

## 22. Web 页面

必须实现 `/login`、`/monitor`、`/robots`、`/maps`、`/parking`、`/tasks`、`/alarms`、`/history`、`/users`、`/audit`、`/settings`。Monitor 显示状态、battery/mode/task/site/map version、二维地图、位置/heading/trajectory/语义点、火情、传感器、任务、manual lease、软件急停和 RGB/thermal/bottom IR 卡片。UI 区分 created/published/accepted/executing/success/failed/unconfirmed。

## 23. MapAdapter/StorageAdapter

MapAdapter 支持 world source、zoom/pan/fit/follow、layers、parking click、polygon/点位编辑、轨迹/历史、报警高亮、版本与 draft/published。StorageAdapter 校验 max size、MIME、扩展名、SHA-256、随机对象名、防路径穿越，原文件名只作元数据；本地持久卷并预留 S3 adapter。

## 24. REST API

统一 `/api/v1`，覆盖 Auth（login/refresh/logout/me/ws-ticket）、Robots（列表/详情/latest/trajectory/connection/capabilities）、Manual Lease、Commands（manual/stop/e-stop/reset/return-dock 与查询）、Sites/Maps/Versions/publish/archive/语义对象/assets、Tasks（list/detail/patrol/extinguish/cancel）、Alarms（list/detail/manual/ack/confirm/dismiss/resolve/create-task）、History（telemetry/sensor/task/command/alarm/audit）、Admin（users/roles/permissions/settings/audit）。

## 25. API Idempotency

创建任务、人工火情和高风险命令支持 `Idempotency-Key`；同 actor + endpoint + key 不重复创建，且相同 key 不允许不同 payload。全链路贯通 request_id、correlation_id、command_id、task_id、event_id。

## 26. Media/WebRTC

MediaMTX 容器真实启动；VideoProvider/WHEP/stream registry 与 DISABLED/OFFLINE/CONNECTING/LIVE/ERROR 五态真实实现。无真实源时显示未配置/OFFLINE，不伪造 LIVE，不向 Web 暴露 RTSP。SERVER 预留 media auth、STUN/TURN、coturn、NAT、TLS。

## 27. Mock Robot

R001 必须是独立 MQTT client，绝不直调 API/DB。频率：location 10 Hz、status 1 Hz、sensor 2 Hz、heartbeat 1 Hz；支持 LWT/capabilities/command/ACK/task_status、patrol/extinguish/manual/stop/e-stop latch/reset/reboot/map mismatch/fire/offline，以及 delay/loss/duplicate/bad JSON/invalid schema/out-of-order/skew/late ACK/duplicate ACK/wrong command_id ACK。

## 28. Protocol Schema 单一事实源

`packages/protocol-schemas` 的 versioned JSON Schema 为唯一 canonical definition；fixtures、Mock、ingress、tester 共用；生成 Python/TypeScript 类型；CI 检测 schema/model drift，禁止人工维护三份定义。

## 29. Protocol Tester

独立于 Web UI，测试 broker/auth、heartbeat/availability/capabilities/location/status/sensor/alarm/task、receive command、accepted/rejected/unsupported ACK、duplicate/out-of-order/boot restart/invalid JSON/unknown schema/TTL expiry/map mismatch，并输出 PASS/FAIL 与诊断。

## 30. Observability

提供 `/health/live`、`/health/ready`、`/metrics`。readiness 按角色检查 PostgreSQL、Redis、MQTT；MediaMTX 单独显示。指标至少覆盖 robot online、MQTT ingress/invalid/duplicate/out-of-order、ACK latency、timeout/unconfirmed、WS connections/resync、task failures、active alarms、DB pool、Redis latency、clock skew。日志使用结构化 JSON 和 request/correlation/vehicle/command/task/event/operator 字段，不打印 secret。

## 31. Telemetry Retention/Partition

正式 schema 中 telemetry/sensor 按时间分区；默认 telemetry 30 天、sensor 90 天、audit 365 天，task/command/fire 不自动删除。retention worker 使用分区或批处理，不以大表逐行慢删作为最终方案。

## 32. Backup/Restore

备份 PostgreSQL、地图/资产、应用设置、Mosquitto ACL/security 配置、MediaMTX/Nginx 配置。必须实际验证空环境 restore 后 login、map、R001 history、alarms、tasks、audit 均恢复。RPO/RTO 标为 `To Be Confirmed by deployment owner`，不得虚构商业 SLA。

## 33. Server Deployment Ready

第二台服务器不在本机，本轮不得声称 SERVER DEPLOYED。提供 server compose、`.env.server.example`、部署/备份文档、防火墙端口、持久卷、restart/log rotation、TLS mounts、MQTT ACL、reverse proxy、健康检查、备份脚本和开机启动指引。推荐 Ubuntu Server + Docker Engine/Compose；Windows 仅兼容说明。

## 34. Remote ROS2 Integration Contract

车端主动 outbound 连接 broker。LAN 配置平台 IP/port/vehicle credential/topics/schema/QoS/LWT；Internet 使用 MQTT TLS 8883、anonymous false、per-robot credential/ACL、可选 client cert，不要求 robot public IP，不公开 ROS2 DDS。R001 只能写自身 telemetry/alarm/status/ACK、读自身 command，不能访问 R002。SSH 运维可用 Tailscale/WireGuard，与业务 MQTT 分离。

## 35. CI/Supply Chain

GitHub Actions 在 develop/main 执行：Backend Ruff/format/type/Pytest/migration；Frontend ESLint/Prettier/type/Vitest/build；Protocol schema/drift/conformance；Containers build/health smoke；Security secret/dependency/container scan。依赖锁定，Actions 固定 commit SHA，镜像禁止 latest。

## 36. Tests

Backend 覆盖 auth rotation/revoke、RBAC、migration、MQTT valid/invalid/duplicate/idempotent、downsample、boot/seq、online 状态、alarm dedup/lifecycle、lease、command/outbox/ACK、task/execution conflict/map mismatch、WS ticket/watermark/resync、audit/health。Frontend 覆盖 store/API/MapAdapter/transform/alarm/command/manual 安全事件/WS reconnect/resync/RBAC。E2E 覆盖登录、R001、地图、lease、manual、stop/ACK、火情、人工事件、灭火状态链、history、permissions。故障覆盖 MQTT/Redis/API/dispatcher/Mosquitto/Mock/DB、boot/seq/skew/duplicate/late/wrong ACK、WS gap、页面隐藏/关闭、双浏览器、offline/e-stop/task/map mismatch、过期不重放和 worker 幂等。

## 37. 本机一键启动

`scripts/dev.ps1` 检查 Docker CLI/Engine，可安全启动 Desktop 时启动并有界等待；否则只提示唯一人工动作。检查 `.env`，compose build/up，migration，seed，Mock，health wait，输出 URL 和一次性 bootstrap 凭据；不把 secret 提交 Git。

## 38. Seed Data

DEV 幂等创建 bootstrap admin、六角色、R001、Site `DEMO_PARKING`、Map `parking_v1` V1、A-01～A-12、inspection/extinguish points、sample trajectory、resolved fire event 和 history。SERVER 不自动插 demo。

## 39. 页面验收

本机实际看到 Login/Monitor、R001 online、10 Hz 平滑位置/heading、双浏览器 lease 互斥、按住前进 Mock 移动、松开 stop、hidden/关闭不继续 manual、自动火情联动、A-12 人工火情、灭火 created→published→accepted→executing→succeeded、ACK timeout unconfirmed、未 ACK e-stop 不显示已急停、map mismatch 拒绝、history/audit/settings 可查、无视频源显示 OFFLINE。

## 40. 本轮不要求真实完成

真实 ROS2、SLAM、Nav2、真车 manual/e-stop/灭火、真实 RTSP/热像、公网域名、正式 CA/TLS、第二台服务器安装、现场 ACL、正式 SLA、消防安全认证属于 NEXT PHASE；写入 `PROTOCOL_TODO.md` 或最终 NEXT INPUTS，不阻塞 Mock baseline，也不得伪造 PASS。

## 41. 开发优先级

P0-A Infrastructure；P0-B Identity；P0-C Realtime；P0-D Control Safety；P0-E Map Monitor；P0-F Mock；P1-A Alarm；P1-B Task；P1-C Map Configuration；P1-D History/RBAC/Audit；P2-A Video；P2-B Observability；P2-C Operations；P2-D Hardening。优先级是实施顺序，不表示删除后续模块。

## 42. Codex 工作规则

完整阅读本文件，维护 `docs/IMPLEMENTATION_TRACKER.md` 的 TODO/IN_PROGRESS/PASS/BLOCKED。不以文档、代码生成或 build 成功替代功能验收；未知实车参数使用 Mock + Adapter + TODO，不询问、不阻塞。只有 Git/SSH 权限、目录覆盖风险、Docker 人工授权、真实硬件已成为唯一剩余项或高风险不可逆动作时暂停。失败测试必须修复或真实 BLOCKED。

## 43. 最终 Git 验收

合并前要求工作区 clean、无 secret/.env/private keys/backups、develop CI/backend/frontend/protocol/E2E/Docker/restore 全通过。之后合并 main、在 main 重跑 smoke、推送 main/develop，记录 MAIN_SHA、DEVELOP_SHA、PUSHED_MAIN、PUSHED_DEVELOP，最终 clean main。

## 44. 最终报告格式

最终必须按以下章节汇报：

- RESULT：Git、Web、API、PostgreSQL、Redis、MQTT、Ingress、Dispatcher、Worker、Mock、WS、Auth/RBAC、Lease、Manual、Stop、E-stop、Map/Version、Alarm、Task、History、Audit、MediaMTX、Backup/Restore、Protocol、E2E、CI。
- RUN：精确 Windows/Docker/login/test 命令。
- URLS：Web、API docs、health、metrics、Media。
- GIT：repo、branch、main/develop SHA、push state、clean status。
- SERVICES：name、image/version、health。
- DATABASE：revision、table/partition/seed。
- MQTT：broker、topics、QoS/retain、R001。
- TESTS：suite、exact command、pass/fail count。
- ACCEPTANCE：逐项 PASS/FAIL/BLOCKED。
- KNOWN LIMITATIONS：只写真限制。
- ROS2 INTEGRATION CONTRACT：字段/topics/QoS/TTL/LWT/ACK。
- NEXT INPUTS：vehicle IDs、capabilities、真实 map、max speed、sensors、video、network、TLS、server host。
- SERVER：明确 `SERVER_DEPLOYMENT_READY=YES/NO`，且未部署第二台机器时 `SERVER_DEPLOYED=NO`。

## 45. 启动指令

开始时检查目标目录、Git/SSH/Docker，正式 remote 使用 `git@github.com:guolichen007/ROBOT-Web.git`。在 `develop` 实施，不开发任何 ROS2/SLAM/Nav2/设备端代码；未知参数以 Mock/Adapter/TODO 隔离。持续完成 Windows 本机真实全栈、测试和浏览器闭环，全部门禁通过后才合并 main。第二台服务器只做到 deployment ready，不伪装 deployed。
