# Implementation Tracker

状态：`TODO` / `IN_PROGRESS` / `PASS` / `BLOCKED`

| 工作流 | 状态 | 证据/备注 |
| --- | --- | --- |
| Git / main / develop | IN_PROGRESS | `develop` 实现与本机门禁已通过，等待最终合并和双分支推送 |
| P0 Infrastructure / Profiles | PASS | DEV/TEST/SERVER 三个 Compose profile 均通过 `config --quiet`；DEV 12 服务健康 |
| P0 Identity / RBAC / Audit | PASS | Argon2id、refresh rotation/revoke、CSRF、WS ticket、六角色和细粒度权限已测试 |
| P0 Protocol / Realtime / Watermark | PASS | Schema 1.1、boot_id/seq、Redis Stream replay/gap/resync 和一次性 ticket 验收通过 |
| P0 Manual Lease / Control Safety | PASS | Redis 原子 lease、manual pulse、stop、e-stop、ACK 与 offline policy 已实现和测试 |
| P0 Map Version / Coordinate Contract | PASS | Site/Map/MapVersion、发布不可变、任务快照和 map mismatch 门禁通过 |
| P0 Mock Robot | PASS | R001 仅通过真实 MQTT 运行；位置、传感器、ACK、任务和故障注入通过 |
| P1 Alarm / Manual Fire / Dedup | PASS | 自动/人工事件、A-12 地图创建、生命周期和重复合并通过 |
| P1 Task / Execution Policy | PASS | patrol/extinguish/return_dock/cancel、outbox、冲突矩阵与状态时间线通过 |
| P1 History / Map Configuration | PASS | 历史查询、地图语义配置、安全资产上传和权限 UI 已实现 |
| P2 Media / Observability | PASS | MediaMTX 实际启动；无源显示 OFFLINE；health/ready/metrics/结构化日志通过 |
| P2 Backup / Restore / Server Package | PASS | 空环境 restore 计数和登录/地图/历史/报警/任务/审计通过；SERVER package 校验通过 |
| Automated tests | PASS | Ruff、format、mypy、22 Pytest、typecheck、ESLint、Prettier、6 Vitest、build 通过 |
| Protocol tests | PASS | 独立 tester 经 Mosquitto 返回 `RESULT PASS count=6`，生成模型 drift 通过 |
| Playwright / browser acceptance | PASS | Chromium 5/5；另完成人工浏览器全部路由和实时监控验收 |
| Fault tests | PASS | MQTT/Redis/API/dispatcher/Mock 重启、离线策略、重复乱序 ACK、过期不重放核心项通过 |
| develop -> main / push | IN_PROGRESS | 仅在最终安全扫描、提交和 main smoke 完成后改为 PASS |
