# CODEX 最终深度迭代任务书
## 智能灭火机器人云控平台 V2 — Integration-Ready Final

本文件是 `guolichen007/ROBOT-Web` 在真实 ROS2 小车接入前的最后一次集中工程化迭代规格。
目标：一次性解决当前已知代码一致性、协议、服务器安全、数据库、媒体鉴权、GitHub 工程规范、中文文档和 ROS2 对接交付问题。
完成全部 Release Gate 后冻结平台功能范围与车云接口；后续除真实实机证据外，不再以 V2.1/V2.2/V2.3 的方式继续拆小版本。

正式仓库：`git@github.com:guolichen007/ROBOT-Web.git`
Windows 工作区：`C:\Users\13576\Desktop\web_robot`
本轮建议 tag：`v2.0.0-integration-ready`
协议最终冻结：`contract_version=1.2.0`，`schema_version=1.2`

---

# 1. 本轮边界

平台负责：Vue Web、FastAPI、PostgreSQL、Redis、Mosquitto、MQTT ingress、Command Dispatcher、Task Worker、WebSocket、MediaMTX 接入、认证/RBAC/审计、机器人/地图/报警/任务/历史、Mock、CI、备份恢复、服务器部署包和 ROS2 接口合同。

禁止开发：ROS2 节点、SLAM、Nav2、传感器驱动、底盘、灭火机构、车端 watchdog、物理急停回路。

Web/平台与车端唯一业务边界：`MQTT + Media Protocol`。

---

# 2. Git 工程流程

从当前最新 `main` 创建：

`hardening/v2-integration-ready-final`

流程：

`hardening/v2-integration-ready-final -> PR -> develop -> CI -> PR -> main -> CI -> tag`

要求：
- 禁止直接在 main 开发。
- 禁止 force push main/develop。
- 禁止提交 `.env`、私钥、证书、密码、备份、真实现场媒体/地图。
- 不得删除失败测试制造 PASS。
- 若权限允许，为 main 建 ruleset：PR 合并、required checks、禁 force push、禁删除。
- required checks 至少：backend、frontend、protocol、containers、e2e、security、codeql。
- solo repo 不强制外部 reviewer 数量，以免自锁。

---

# 3. 本轮必须修完的已知问题

## FIX-01 PostgreSQL commit 与 Redis realtime event 顺序

当前风险：DB transaction 未 commit 时已向 Redis Stream 发布 `command.updated`，若 commit 失败，Web 可看到不存在的 PUBLISHED 状态。

修改：
- 所有“业务事实状态”先成功 commit PostgreSQL，再发布 Redis realtime event；
- 或使用轻量 realtime-event outbox；
- 不引入 Kafka。

验收：
- 人为注入 DB commit failure；
- WebSocket/Redis 不得看到 rollback 的成功状态。

## FIX-02 禁止服务器伪造 boot_id

删除类似：

`robot.boot_id or str(uuid4())`

最终语义：
- Vehicle -> Platform：`boot_id`
- Platform -> Vehicle：`target_boot_id`

普通 manual/stop/reset/patrol/extinguish/return_dock/cancel：
- 无 current boot session -> 拒绝；
- 错误码 `ROBOT_BOOT_SESSION_UNKNOWN`。

software e-stop：
- 可允许 `target_boot_id=null` 作为安全特殊情况，但必须写入协议，并始终等待车端 ACK。

本轮将协议一次性升级为 `1.2 / 1.2.0`，之后冻结。

## FIX-03 MediaMTX 鉴权必须真实

DEV：
- Media API 仅本机调试；
- 不再宣称“生产认证已启用”；
- 9997 不做非必要外部暴露。

SERVER：
- Web 用户必须经过平台授权后才能取得短时 media ticket；
- ticket 绑定 user + robot + camera + expiry；
- 未登录/无权限/过期必须 401/403；
- `/media/` 不能仅凭 URL 即可播放；
- MediaMTX 管理 API 不公网暴露。

test profile 增加 H.264 测试源，验证 authorized/unauthorized/expired WHEP。

## FIX-04 Server healthcheck

SERVER critical services 全部具备 health 或可靠 heartbeat：
postgres、redis、mosquitto、api、web、nginx、mediamtx、mqtt-ingress、command-dispatcher、task-worker。

新增：
- `scripts/server-preflight.sh`
- `scripts/server-preflight.ps1`

检查 Docker、Compose、CPU/RAM/disk、端口、secrets、TLS、MQTT ACL、hostname、time sync、backup path、placeholder。

## FIX-05 Server 暴露面

公网默认仅：
- 80 -> 443 redirect
- 443 HTTPS/WSS/WebRTC gateway
- 8883 MQTT TLS

限制：
- `/health/live` 仅最小结果；
- `/health/ready` 管理网/VPN/内部；
- `/metrics` 不公网；
- SERVER `ENABLE_API_DOCS=false`；
- MediaMTX admin API 不公网。

Nginx 补齐 HSTS、X-Content-Type-Options、Referrer-Policy、CSP/frame-ancestors、Permissions-Policy、server_tokens off。

## FIX-06 真正时间分区

建立：
- `telemetry_samples_YYYY_MM`
- `sensor_samples_YYYY_MM`

要求：
- 当前月 + 未来至少 2 月自动创建；
- default partition 仅异常兜底；
- default 有数据时 metric + warning；
- retention worker 维护历史分区；
- 跨月、retention、restore 测试。

## FIX-07 Redis consumer name 唯一

禁止写死 `dispatcher-1`。

使用：
`service-name + hostname + process-uuid`

测试 2 dispatcher 实例 consumer 不冲突。

## FIX-08 Server secrets

SERVER 优先 Docker secrets / `*_FILE`：
PostgreSQL、Redis、JWT、refresh、CSRF、bootstrap admin、MQTT password、TLS private key/CA。

`.env.server.example` 仅非 secret 配置/secret path。
存在 `REPLACE_` / `CHANGE_ME` / 示例密码时 server preflight fail-fast。

## FIX-09 稳定 machine-readable 错误

统一 API 错误：

```json
{
  "error": {
    "code": "ROBOT_OFFLINE",
    "message": "机器人当前离线",
    "request_id": "...",
    "details": {}
  }
}
```

至少固定：
`ROBOT_DISABLED`
`ROBOT_STALE`
`ROBOT_OFFLINE`
`ROBOT_BOOT_SESSION_UNKNOWN`
`ROBOT_CAPABILITY_UNSUPPORTED`
`ROBOT_ESTOP_ACTIVE`
`MANUAL_LEASE_CONFLICT`
`MANUAL_LEASE_INVALID`
`ACTIVE_TASK_CONFLICT`
`MAP_VERSION_MISMATCH`
`COMMAND_EXPIRED`
`COMMAND_ACK_TIMEOUT`
`COMMAND_REJECTED`
`COMMAND_UNSUPPORTED`
`AUTH_REQUIRED`
`PERMISSION_DENIED`
`INVALID_PROTOCOL_MESSAGE`
`PROTOCOL_VERSION_UNSUPPORTED`

ROS2 `command_ack.reason_code` 使用稳定枚举。

## FIX-10 MQTT ingress 防护

增加：
- `MAX_MQTT_PAYLOAD_BYTES`
- Broker message size limit
- JSON size/depth 防护
- invalid message/rate metrics
- 拒绝 camera Base64/video payload
- 高频保护不得误伤正常 location 10 Hz。

## FIX-11 History 1.12MB chunk

本轮消除已知 chunk warning：
- route lazy loading
- History dynamic import
- ECharts 按需/异步
- 合理 split/bundle budget

不做无意义极限微优化。

## FIX-12 前端实时/控制异常统一

确认并测试：
- WS exponential backoff + jitter
- reconnect snapshot/resync
- stale/offline banner
- lease lost disable manual
- hidden/blur/logout stop + release
- beforeunload 不是最终安全保障，TTL 才是
- 多浏览器 lease conflict UI
- ACK timeout / late / duplicate / wrong command id
- server 503/断网绝不显示成功

---

# 4. Repo-wide 深度审查要求

Codex 不能只修 FIX-01~12。

先创建：

`docs/DEEP_HARDENING_TRACKER.md`

对整个仓库审查：
- Python
- TypeScript/Vue
- SQLAlchemy/Alembic
- Redis
- MQTT
- Nginx
- MediaMTX
- Compose
- CI
- scripts
- tests
- docs

任何“可复现的平台自身问题”本轮加入 tracker 并修掉，不得转成后续 V2.x。

最终：
`Remaining platform defects = 0`

允许剩余的只能是外部真实设备/现场输入。

---

# 5. ROS2 Integration Contract 1.2.0 最终冻结

Topic：

`robot/{vehicle_id}/availability`
`robot/{vehicle_id}/heartbeat`
`robot/{vehicle_id}/capabilities`
`robot/{vehicle_id}/location`
`robot/{vehicle_id}/status`
`robot/{vehicle_id}/sensor`
`robot/{vehicle_id}/alarm`
`robot/{vehicle_id}/task_status`
`robot/{vehicle_id}/command`
`robot/{vehicle_id}/command_ack`

QoS/retain：

| Topic | Direction | QoS | Retain | Nominal |
|---|---|---:|---|---|
| availability | vehicle->platform | 1 | YES | connect/LWT |
| heartbeat | vehicle->platform | 0 | NO | 1Hz |
| capabilities | vehicle->platform | 1 | YES | boot/config |
| location | vehicle->platform | 0 | NO | 5-10Hz，推荐10Hz |
| status | vehicle->platform | 1 | NO | 1Hz |
| sensor | vehicle->platform | 0 | NO | 1-2Hz |
| alarm | vehicle->platform | 1 | NO | event |
| task_status | vehicle->platform | 1 | NO | state/progress |
| command_ack | vehicle->platform | 1 | NO | event |

command：
- manual_control QoS0
- stop_motion QoS1
- emergency_stop QoS1
- reset_estop QoS1
- patrol QoS1
- extinguish QoS1
- return_dock QoS1
- cancel_task QoS1
- **全部 retain=false**

Vehicle 公共 envelope：

```json
{
  "schema_version": "1.2",
  "message_id": "UUID",
  "type": "location",
  "vehicle_id": "R001",
  "boot_id": "UUID_PER_BOOT",
  "timestamp": "UTC ISO8601",
  "seq": 12345
}
```

`seq` 在 `boot_id + topic` 内单调递增。
Server 记录 `server_received_at` / `clock_skew_ms`。

坐标：
- frame_id map
- x/y m
- theta rad
- theta0 +X
- positive CCW
- linear m/s
- angular rad/s
- temp °C
- battery 0..100%

location 还需：
site_code、map_code、map_version、map_checksum、frame_id。

Command：

```json
{
  "schema_version": "1.2",
  "message_id": "UUID",
  "type": "command",
  "vehicle_id": "R001",
  "target_boot_id": "CURRENT_BOOT_UUID",
  "command_id": "C...",
  "correlation_id": "UUID",
  "task_id": "T...",
  "issued_at": "...",
  "expires_at": "...",
  "ttl_ms": 5000,
  "priority": 50,
  "source": "WEB",
  "cmd": "patrol",
  "params": {}
}
```

Manual：

```json
{
  "cmd": "manual_control",
  "target_boot_id": "...",
  "lease_id": "...",
  "control_session_id": "...",
  "seq": 120,
  "ttl_ms": 500,
  "params": {
    "linear_x": 0.30,
    "angular_z": 0.00
  }
}
```

车端必须使用本地 monotonic receive time 做 TTL watchdog。

ACK：

```json
{
  "schema_version": "1.2",
  "type": "command_ack",
  "vehicle_id": "R001",
  "boot_id": "...",
  "command_id": "C...",
  "task_id": "T...",
  "status": "accepted",
  "reason_code": null,
  "reason": null
}
```

status 只允许：
accepted / rejected / unsupported

accepted = 车端应用层完成本地状态/参数校验并接受执行，不是 MQTT 收包。

task_status：
accepted / executing / completed / failed / cancelled。
phase 可扩展，未知 phase 不得使平台崩溃。

---

# 6. ROS2 对接文件包：必须真实输出

新增：

```text
integration/ros2/
├─ README_现场对接说明.md
├─ ROBOT_INTEGRATION_MANIFEST.json
├─ ROS2_MQTT接口合同.md
├─ ROS2_字段字典.csv
├─ ROS2_对接参数模板.yaml
├─ ROS2_验收清单.md
├─ MAP坐标系合同.md
├─ VEHICLE安全责任合同.md
├─ examples/
├─ schemas/
└─ test-vectors/
```

examples 必须覆盖：
availability、heartbeat、capabilities、location、status、sensor、fire_alert、task_status、manual_control、stop_motion、emergency_stop、reset_estop、patrol、extinguish、return_dock、cancel_task、ACK accepted/rejected/unsupported。

`ROBOT_INTEGRATION_MANIFEST.json` 是 machine-readable 总合同，包含：
- contract/schema version
- broker dev/server placeholder
- identity
- coordinate contract
- topic/direction/qos/retain/frequency/schema
- commands/qos/ttl/idempotency
- ACK semantics
- map/version rules
- time rules
- safety rules

`ROS2_对接参数模板.yaml` 只让现场填真实值：

```yaml
vehicle:
  vehicle_id: TODO
  site_code: TODO
  map_code: TODO
  map_version: TODO
  map_checksum: TODO

mqtt:
  host: TODO
  port: 8883
  tls: true
  username: TODO
  ca_file: TODO

frames:
  map: map
  base: base_link

motion:
  max_linear_x_mps: TODO
  max_angular_z_radps: TODO

ros_mapping:
  localization_source: TODO
  battery_source: TODO
  smoke_source: TODO
  bottom_ir_source: TODO
  top_ir_source: TODO
  command_target: TODO
  estop_target: TODO

video:
  roof_rgb: TODO
  roof_thermal: TODO
  bottom_ir: TODO

time_sync:
  method: TODO_NTP_CHRONY_PTP
```

平台不得替现场猜 ROS topic。

新增：
- `scripts/build-ros2-handoff.ps1`
- `scripts/build-ros2-handoff.sh`

输出：
- `dist/firebot-ros2-integration-1.2.0/`
- `dist/firebot-ros2-integration-1.2.0.zip`
- `dist/firebot-ros2-integration-1.2.0.sha256`

GitHub Actions 增加 integration-package artifact。

---

# 7. ROS2 现场首次对接顺序

文档固定 Gate：

1. Broker/TLS/identity
2. availability/heartbeat/capabilities
3. location/map/version/x/y/theta
4. status/sensor/time
5. command_id/ACK
6. stop_motion（安全区域）
7. low-speed manual + 500ms TTL + 断网
8. software e-stop + latch/reset
9. patrol/Nav
10. fire alert
11. extinguish task
12. 最后才开放真实灭火执行机构

前一 Gate 不通过，下一 Gate 不启用。

---

# 8. GitHub 文档必须全面中文工程化

根 `README.md` 必须包含：
1. 项目简介
2. 职责边界
3. 当前版本/状态
4. Mermaid 架构
5. 功能矩阵
6. 技术栈
7. 目录
8. Windows 快速启动
9. Bootstrap 登录
10. URLs
11. 停止/重置
12. 测试
13. DEV/TEST/SERVER
14. Mock Robot
15. ROS2 边界
16. 服务器部署
17. 安全说明
18. 视频状态
19. Troubleshooting
20. docs 索引
21. Git/贡献流程
22. License 状态

新增/完善：

```text
docs/README.md
docs/ARCHITECTURE.md
docs/GETTING_STARTED_WINDOWS.md
docs/DEVELOPMENT.md
docs/TESTING.md
docs/SERVER_DEPLOYMENT_UBUNTU.md
docs/OPERATIONS.md
docs/TROUBLESHOOTING.md
docs/DATABASE.md
docs/SECURITY.md
docs/BACKUP_RESTORE.md
docs/MQTT_PROTOCOL.md
docs/MAP_COORDINATE_CONTRACT.md
docs/VEHICLE_SAFETY_CONTRACT.md
docs/RELEASE_CHECKLIST.md
docs/API_REFERENCE.md
CONTRIBUTING.md
CHANGELOG.md
SECURITY.md
CODEOWNERS
.github/PULL_REQUEST_TEMPLATE.md
.github/ISSUE_TEMPLATE/bug_report.yml
.github/ISSUE_TEMPLATE/feature_request.yml
.github/ISSUE_TEMPLATE/robot_integration.yml
.github/dependabot.yml
```

新增 CodeQL（Python + JS/TS）、markdown link check、docs consistency、integration package build。

LICENSE：不得擅自选择 MIT/Apache/GPL。
若 owner 未决定，README 明确“当前未声明开源许可证，未经授权不得复制/分发/用于其他项目”，并把 License choice 列为 OWNER_DECISION。

---

# 9. SERVER 文档必须能从空 Ubuntu 照着部署

`docs/SERVER_DEPLOYMENT_UBUNTU.md` 至少写：
系统要求、Docker、clone、checkout tag、secrets、MQTT CA/server cert、password/ACL、env、preflight、compose up、migration、admin bootstrap、Nginx TLS、firewall、health、browser、robot MQTT test、backup、restart、update、rollback、logs、shutdown。

命令必须可复制。
所有 TODO/REPLACE 必须解释。

Windows 文档从干净环境写：
Git、Docker Desktop、WSL2、clone、env、dev.ps1、URL、bootstrap、test、stop/reset、PowerShell policy、端口、firewall、更新。

Troubleshooting 至少覆盖：
8080、Docker、Postgres、Redis、MQTT、R001 offline、Mock 无位置、WS、lease conflict、ACK timeout、map mismatch、wrong boot、Media offline、backup、TLS。

---

# 10. GitHub Release Gate

必须 PASS：

Code：
- Ruff/format/mypy
- ESLint/Prettier/vue-tsc
- production TODO/FIXME/HACK=0（明确 allowlist 除外）
- no secret
- no latest image
- no broken docs links
- no stale tracker

Tests：
- backend
- frontend
- protocol
- E2E
- fault
- media auth
- DB commit/event consistency
- boot/target_boot
- partition
- backup/restore
- server preflight
- handoff package conformance

Security：
- Gitleaks
- Trivy CRITICAL=0
- Trivy HIGH 输出报告
- CodeQL
- dependency review
- server public-surface check

Compose：
- dev config/full stack
- test config/full stack
- server config/server-profile smoke
- server smoke 只代表本机模拟，不得声称真实第二台服务器部署。

---

# 11. 稳定性 Gate

至少执行：

### 1-hour R001 soak
10Hz location、1Hz status、1Hz heartbeat、2Hz sensor。

要求：
- 无崩溃
- 无 outbox 异常积压
- Redis event backlog 不无界
- telemetry 正确分区
- default partition 无异常增长
- memory 无明显持续线性增长

### Burst
10 robots × 10Hz location × 10 minutes。

仅用于基本架构验证，不宣称正式容量 SLA。

输出 `docs/SOAK_TEST_REPORT.md`。

---

# 12. Definition of Done

最终 `KNOWN_LIMITATIONS` 只允许：
- 真实 ROS2/底盘/传感器/灭火机构未接
- 真实视频未接
- 第二台服务器未实际部署
- 正式域名/TLS/ACL 值待现场
- RPO/RTO owner 未确认
- 真实速度/量程/map 待现场
- License owner decision

不允许剩余：
已知代码 bug、fake boot_id、event consistency、Media 鉴权、metrics/docs 暴露、health 缺失、default partition 长期使用、fixed consumer name、History chunk warning、README/docs 缺失、broken links、stale docs、测试跳过、协议/schema/examples 漂移。

发现新的平台小问题，本轮继续修，不再命名后续 V2.x。

---

# 13. 最终执行顺序

1. 读完整规格。
2. 检查 current main / clean state。
3. 创建 hardening branch。
4. 建 `DEEP_HARDENING_TRACKER.md`。
5. repo-wide audit。
6. 修 FIX-01~12 + audit 发现的问题。
7. 升级并冻结 protocol 1.2。
8. 生成 ROS2 handoff。
9. GitHub 中文文档工程化。
10. 扩 CI/CodeQL/docs/security。
11. 全部自动测试。
12. full stack。
13. fault tests。
14. soak/load。
15. backup/restore。
16. 新目录 clean clone 按 README 从头启动验证。
17. PR -> develop。
18. develop CI。
19. PR -> main。
20. main CI。
21. tag `v2.0.0-integration-ready`。
22. 生成/校验 ROS2 zip + sha256。
23. push refs。
24. 最终 clean main。

---

# 14. 最终报告

Codex 必须输出：

```text
RELEASE
- Release Name
- Tag
- Contract Version
- Schema Version

GIT
- Repo
- Hardening Branch
- Develop SHA
- Main SHA
- Tag SHA
- Push State
- Worktree
- Branch Protection

FIXES
- FIX-01..FIX-12 每项 PASS/FAIL + files + tests

REPO_AUDIT
- Additional issues found
- Additional issues fixed
- Remaining platform defects = 0

FULL_STACK
- DEV
- TEST
- SERVER PROFILE SMOKE

DATABASE
- migration
- partitions
- default partition rows
- retention
- backup/restore

MQTT
- protocol
- topics
- QoS/retain
- boot/target_boot
- ACK

MEDIA
- DEV state
- SERVER auth
- unauthorized/authorized/expired tests

SECURITY
- Gitleaks
- Trivy Critical
- Trivy High report
- CodeQL
- Public surface

TESTS
- Pytest
- Vitest
- Playwright
- Protocol
- Fault
- Soak
- Load
- Clean Clone Smoke

DOCS
- README
- docs index
- Windows
- Server
- Troubleshooting
- Security
- Contribution
- Changelog
- GitHub templates

ROS2_HANDOFF
- Directory
- Manifest
- YAML template
- Schemas
- Examples
- Field dictionary
- Acceptance checklist
- ZIP
- SHA256
- CI artifact

SERVER
- SERVER_DEPLOYMENT_READY=YES/NO
- SERVER_DEPLOYED=NO

KNOWN_LIMITATIONS
- 只允许本文件 Definition of Done 中的外部依赖项

OWNER_DECISIONS
- License
- RPO/RTO
- Production hostname
- Final TLS/CA
```

---

# 15. 给 Codex 的直接启动语

完整阅读本文件，把它作为当前 ROBOT-Web 在真实 ROS2 接入前的最后一次集中工程化迭代规格。

不要再创建 V2.1、V2.2、V2.3 等不断追加的小版本。

从当前 main 创建：
`hardening/v2-integration-ready-final`

必须完成：
- 所有已知一致性/协议/服务器/媒体/数据库问题；
- repo-wide 深度审查并修复所有可复现平台问题；
- 中文 GitHub README/docs 工程化；
- Protocol 1.2.0 freeze；
- 可直接交 ROS2 现场人员的 machine-readable 接口包；
- 全栈、故障、媒体鉴权、备份、soak、load、clean-clone；
- PR/CI 合入 main；
- tag `v2.0.0-integration-ready`。

严禁开发 ROS2/SLAM/Nav2/底盘/传感器/灭火机构/车端 watchdog。
现场未知项只填入 `ROS2_对接参数模板.yaml` 和 OWNER/NEXT INPUTS，不阻塞平台工程化。

发现新的平台代码缺陷、小错误、文档矛盾或测试漏洞，本轮直接加入 tracker 修完，不得简单转成 KNOWN_LIMITATION。

直到所有 Release Gate 满足才允许合入 main/tag。
