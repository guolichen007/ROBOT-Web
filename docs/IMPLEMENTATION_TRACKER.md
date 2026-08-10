# Implementation Tracker

状态：`TODO` / `IN_PROGRESS` / `PASS` / `BLOCKED`。本文件只记录已经有可复核证据的状态；本地通过不等同于远端 GitHub Actions 通过。

| 工作流 | 状态 | 证据 / 备注 |
| --- | --- | --- |
| Git / 分支流程 | IN_PROGRESS | `develop` 已提交并推送；全部本地门禁和远端 CI 已通过，正在进入 `develop → main` 发布门禁。 |
| P0 Infrastructure / Profiles | PASS | DEV/TEST/SERVER 三个 Compose 配置通过；DEV 和隔离 TEST 全栈均实际启动。 |
| P0 Identity / RBAC / Audit | PASS | Argon2id、refresh family rotation/revoke、CSRF、登录限流、一次性 WS ticket、六角色/细权限和审计有自动测试。 |
| P0 Protocol / Realtime | PASS | Schema 1.1、boot_id/seq、source/server time、duplicate/out-of-order、latest/downsample、heartbeat 状态已实现。 |
| P0 Snapshot / Delta | PASS | Redis Stream watermark、ticket+after replay、gap/resync 有集成测试。 |
| P0 Manual Lease / Session | PASS | Redis 原子 TTL、一车一租约、seq、续租、logout/权限/WS/hidden 释放与 QoS1 stop 有测试。 |
| P0 Command / ACK / Outbox | PASS | manual/stop/e-stop/durable 分类、ACK 状态、同 command_id 重试、transactional outbox 已验证。 |
| P0 Execution Policy / Offline | PASS | manual/auto/e-stop 冲突、capability、STALE/OFFLINE 和 map mismatch 门禁有测试。 |
| P0 Map Version / Coordinate | PASS | Site/Map/MapVersion、PUBLISHED 不可变、语义对象、任务快照和坐标合同已实现。 |
| P0 Mock Robot | PASS | R001 仅通过真实 MQTT；10 Hz location、status/sensor/heartbeat、任务/控制、e-stop latch、fault injection。 |
| P1 Alarm / Manual Fire / Dedup | PASS | 自动/人工事件、NEW→CONFIRMED→DISPATCHED→IN_PROGRESS→RESOLVED、A-12 与去重测试通过。 |
| P1 Tasks | PASS | patrol/extinguish/return_dock/cancel、时间线、ACK/task_status、执行冲突与 history。 |
| P1 Map Config / Asset Upload | PASS | draft/publish/archive、车位/点位/轨迹与 size/MIME/ext/SHA/random-name/path traversal 检查。 |
| P1 History / RBAC / Audit UI | PASS | 页面与 API 可查询 telemetry/sensor/task/command/alarm/audit。 |
| P2 Media | PASS | MediaMTX `1.18.2` 实际运行；VideoProvider/stream 五态；无源严格显示 OFFLINE。1.20.0 registry tag 不可取得，未伪造该版本。 |
| P2 Observability | PASS | live、ready、metrics、结构化日志；PostgreSQL/Redis/MQTT 故障时 ready 503，恢复后 200。 |
| P2 Backup / Restore | PASS | `20260810T123811Z` 备份后重建空 DEV 数据库恢复；计数 `1|1|24226|13|3|113|19`，全栈重新健康。 |
| P2 Server Package | PASS | server compose、env example、TLS/ACL/coturn/反代/持久化/日志轮转/部署和恢复文档；未实际部署第二台服务器。 |
| Alembic | PASS | 初始 revision `20260810_0001`；隔离 TEST 实测 `head → base → head`，35 个 public tables，seed 幂等。 |
| Backend / Protocol automated | PASS | Ruff、Ruff format、mypy、Pytest `30 passed`；protocol drift；真实 broker tester `RESULT PASS count=11`。 |
| Frontend automated | PASS | ESLint、Prettier、vue-tsc、Vitest `6 passed`、Vite production build。 |
| Playwright E2E | PASS | Chromium `6 passed`：登录、monitor、双租约、manual/hidden safety、自动/人工火情、灭火任务状态链。 |
| 真实浏览器验收 | PASS | 实际浏览器确认 R001 ONLINE、V1 PUBLISHED、12 车位、实时位置/传感器、history/settings、三路视频 OFFLINE，console 0 error。 |
| Core fault tests | PASS | Redis/MQTT/PostgreSQL 503→200；API/dispatcher restart；outbox queued→ACK_ACCEPTED；Mock boot_id 变化；协议异常/TTL/map mismatch。 |
| Secret / vulnerability scan | PASS | Gitleaks 0 leaks；Trivy repo CRITICAL=0；API image CRITICAL=0；Web image CRITICAL=0。 |
| CI definition | PASS | 六 jobs：backend、frontend、protocol、containers+image scan、e2e、security；Actions 固定 SHA。 |
| Remote GitHub Actions | PASS | develop run `31397474188`：backend/frontend/protocol/containers/security/e2e 全部 success。 |
| develop → main / push | TODO | 仅在最终本地门禁和远端 CI 通过后执行。 |

## 当前门禁

- 本地功能、Docker、浏览器、故障、恢复与安全门禁：`PASS`。
- 待完成：提交本跟踪记录、确认新的 develop SHA CI、合并 main、main smoke、双分支 push、clean main。
- `SERVER_DEPLOYMENT_READY=YES`
- `SERVER_DEPLOYED=NO`
