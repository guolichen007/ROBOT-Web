# 深度工程化审查追踪表

本表对应 `Integration-Ready Final` 集中迭代。状态仅允许 `TODO`、`IN_PROGRESS`、`PASS`、`BLOCKED`；只有代码、自动测试和运行证据齐全后才能标记 `PASS`。

## 任务书 FIX-01～FIX-12

| 编号 | 状态 | 可复现问题 | 完成证据 |
| --- | --- | --- | --- |
| FIX-01 | PASS | dispatcher、ingress、task-worker 在 PostgreSQL transaction commit 前调用 Redis Stream `append_event`，回滚时会暴露不存在的业务状态。 | after-commit event queue、回滚清理及 commit-failure 自动测试通过。 |
| FIX-02 | PASS | 平台 command payload 使用 `robot.boot_id or uuid4()`，且错误地发送 `boot_id` 而非 `target_boot_id`。 | migration、boot session、wrong/unknown boot 与 e-stop null 特例测试通过。 |
| FIX-03 | PASS | Media 仅有 stream list；DEV 暴露 9997，SERVER `/media/` 无平台 ticket 鉴权。 | H.264 test source 与 unauthorized/authorized/expired Playwright 用例通过。 |
| FIX-04 | PASS | SERVER 只有 postgres/redis healthcheck；其余关键服务无 health 或 heartbeat gate。 | service heartbeat、Windows/Linux preflight、SERVER compose smoke 通过。 |
| FIX-05 | PASS | SERVER nginx 公开 ready、metrics、API docs 和无鉴权 media；安全响应头不完整。 | public-surface 自动检查通过；SERVER docs/ready/metrics 均不公开。 |
| FIX-06 | PASS | telemetry/sensor 只有 default partition；retention worker 逐行删除。 | 当前月及未来两个月真实分区、跨月/retention/default 指标、restore 断言通过。 |
| FIX-07 | PASS | Redis Stream consumer 固定为 `dispatcher-1`。 | 基于实例 ID 的唯一 consumer 与双实例单元测试通过。 |
| FIX-08 | PASS | SERVER PostgreSQL/Redis secret 仍通过 `.env`，示例含 `REPLACE_*` 值但没有 preflight fail-fast。 | Docker secrets、`*_FILE`、权限与 placeholder fail-fast 测试通过。 |
| FIX-09 | PASS | FastAPI 各模块直接返回不稳定 `detail` 字符串/对象，没有统一 machine-readable error envelope。 | 统一 error envelope、固定 error_code/ACK reason_code 集成测试通过。 |
| FIX-10 | PASS | ingress 无 payload byte/depth/base64 video/rate 防护，broker 无 message_size_limit。 | byte/depth/Base64/rate/10 Hz 边界测试通过；长跑验证高频去重 TTL 稳态。 |
| FIX-11 | PASS | History 顶层导入完整 ECharts，production build 产生约 1.12 MB chunk warning。 | route/chart lazy load；History chunk 约 509 KiB 且 bundle budget 通过。 |
| FIX-12 | PASS | WS reconnect 固定 1.5 秒、无 jitter；实时/租约/ACK 异常 UI 行为未统一覆盖。 | backoff+jitter、gap resync、lease fail-safe 的 Vitest 与 Playwright 用例通过。 |

## Repo-wide 审查新增问题

| 编号 | 状态 | 范围 | 可复现问题 | 修复要求 |
| --- | --- | --- | --- | --- |
| AUDIT-01 | PASS | MQTT ingress | message dedup/seq Redis key 在 DB commit 前写入；commit 失败后重发会被当成 duplicate/out-of-order，形成永久数据丢失。 | pending key 失败清理，成功 commit 后固化 seq/dedup；commit failure 后可重试测试通过。 |
| AUDIT-02 | PASS | boot session | 任何合法旧 boot 消息都能再次覆盖 `robots.boot_id`，重启后的旧包可能回滚当前会话。 | 仅会话消息建立新 boot；旧 boot 拒绝、计量与测试通过。 |
| AUDIT-03 | PASS | manual lease | Redis lease 在 PostgreSQL session/audit commit 前建立；commit failure 会留下无 DB 摘要的有效租约。 | commit failure 补偿删除 Redis lease；无孤儿 lease 测试通过。 |
| AUDIT-04 | PASS | protocol/task | 平台内部使用大写 task status，当前 wire schema/Mock 对状态语义不够明确，与 1.2 小写合同漂移。 | wire 小写枚举与内部状态边界映射、兼容性测试通过。 |
| AUDIT-05 | PASS | containers | Python API/worker/ingress/dispatcher/mock 默认 root 运行。 | Python 镜像固定 UID/GID 10001 非 root，容器运行检查通过。 |
| AUDIT-06 | PASS | SERVER secrets | `DATABASE_URL`、`POSTGRES_PASSWORD`、`REDIS_PASSWORD` 仍以普通环境变量传递，未完整使用 Docker secrets。 | Docker secrets 与配置读取器完成，SERVER 实际 smoke 通过。 |
| AUDIT-07 | PASS | SERVER bootstrap | API docs 总是启用，`ENABLE_API_DOCS=false` 没有配置入口。 | SERVER docs/openapi 关闭且公开面扫描通过。 |
| AUDIT-08 | PASS | health/operations | Media 状态只做 TCP 9997 探测，无法区分鉴权、provider 和 stream 状态；关键 worker readiness 未集中核验。 | 角色 heartbeat、Media API 状态、设置页与 preflight 对齐。 |
| AUDIT-09 | PASS | backup/restore | 备份只复制部分配置且不记录 migration/partition manifest；restore 未断言月分区/default rows。 | manifest/migration/partition 完整；实际清空、恢复及业务 HTTP smoke 通过。 |
| AUDIT-10 | PASS | CI/docs | 缺 CodeQL、dependency review、docs link/consistency、TODO gate、no-latest gate、public-surface、handoff artifact。 | CI、CodeQL、dependency review、文档/仓库政策/公开面/handoff gate 已加入。 |
| AUDIT-11 | PASS | GitHub/docs | README 和 docs 不满足中文工程索引；缺贡献、安全披露、变更日志、CODEOWNERS、PR/Issue 模板、Dependabot。 | 中文 README/docs 与全部社区工程文件完成，链接检查通过。 |
| AUDIT-12 | PASS | supply chain | Python production image 未以非 root 用户运行；GitHub workflow 三方 action pinning 不完整范围未自动验证。 | non-root 容器与 action SHA/pinned image 政策检查通过。 |
| AUDIT-13 | PASS | protocol package | canonical schema、Python/TS models、fixtures、Mock、tester、文档仍为 1.1，缺 machine-readable 1.2.0 handoff。 | schema 1.2、contract 1.2.0、模型漂移与 handoff conformance 通过。 |
| AUDIT-14 | PASS | stability | 无一小时 soak、多车 burst、default partition 增长和内存趋势报告。 | 36,000 location soak 与 60,000 location burst 均 PASS；详见 `SOAK_TEST_REPORT.md`。 |
| AUDIT-15 | IN_PROGRESS | clean clone | 尚无从干净 clone 按 README 完成启动的自动 smoke。 | 待 hardening commit/push 后从远端新目录执行。 |
| AUDIT-16 | PASS | migrations | 旧 migration 与 ORM metadata 存在 UUID/default 定义兼容差异。 | UUID 兼容迁移与空库/上一 revision upgrade 测试通过。 |
| AUDIT-17 | PASS | MQTT ingress | ingress 误订阅 command topic，扩大不必要的信任面。 | 仅订阅车端上行 topic，协议不变量测试通过。 |
| AUDIT-18 | PASS | SERVER startup | Mosquitto secret 配置权限及 worker 对 migrate 的启动条件会造成冷启动失败。 | config-init 权限修复、`service_completed_successfully` 依赖和全新 volume smoke 通过。 |
| AUDIT-19 | PASS | operations | PowerShell 5 对 Alembic stderr 与 JSON 单元素数组处理导致 backup/restore 误判。 | DB migration revision 直查、强制字符串数组；实际备份恢复通过。 |
| AUDIT-20 | PASS | load safety | 高频 `message_id` 去重统一保留 24 小时，10 Hz 长跑造成 Redis 键线性增长。 | 按 topic 分层 TTL；稳态 key/内存观测与自动测试通过。 |
| AUDIT-21 | PASS | supply chain | Axios、Vite、PyJWT、python-multipart、Starlette 版本存在可修复 HIGH 漏洞。 | 升级到兼容固定版本，文件系统及镜像 Trivy 复扫纳入 Release Gate。 |
| AUDIT-22 | PASS | manual control | 页面 hidden/pagehide 时普通 Axios 请求可能被浏览器中止，服务端 lease 要等 TTL 才释放。 | stop-motion 与 lease DELETE 改为 keepalive 请求；Playwright 从第二客户端验证服务端租约已立即释放。 |
| AUDIT-23 | PASS | dependency recovery | Redis 重启会清空角色心跳；PostgreSQL 短停的未捕获异常会终止 dispatcher，且结构化日志丢失 traceback/错误服务名。 | worker/dispatcher 周期续约心跳、dispatcher 依赖故障自愈、日志保留 service/exception；Redis 与 PostgreSQL 均实测 503→200。 |
| AUDIT-24 | PASS | container security | Debian slim 基础镜像含无法由项目依赖升级消除的 HIGH/CRITICAL OS 漏洞。 | production/test Python 基础镜像切换为固定 `python:3.12.13-alpine3.24`；最终 API/Web 镜像与仓库依赖 Trivy HIGH=0、CRITICAL=0。 |
| AUDIT-25 | PASS | SERVER migration | Alembic `ConfigParser` 会把数据库密码 URL 中合法的 `%xx` 编码当作插值语法，SERVER 冷启动迁移失败。 | 对 Alembic 配置值转义 `%` 并增加回归测试；含 `%21` 密码的全新 SERVER volume smoke 通过。 |
| AUDIT-26 | PASS | SERVER MQTT health | Mosquitto healthcheck 订阅 ACL 未授权的 `$SYS` topic，合法 TLS/auth 连接仍永久 unhealthy。 | 健康检查改为 ACL 允许的 QoS1 publish；全新 SERVER profile 所有关键服务 healthy。 |
| AUDIT-27 | PASS | release tooling | 仓库政策脚本会递归扫描 ignored 的 Trivy 构建上下文和本地虚拟环境，产生与可提交源无关的假阳性。 | 明确排除 `artifacts` 与 `.venv`；可提交源、固定镜像和 CI action 政策检查通过。 |

## 审查覆盖面

- [x] Python/FastAPI/SQLAlchemy 初审
- [x] Redis/MQTT/dispatcher/worker 初审
- [x] TypeScript/Vue/Vite 初审
- [x] Alembic/partition/backup 初审
- [x] Compose/Nginx/MediaMTX/SERVER 初审
- [x] GitHub Actions/测试/文档初审
- [x] 修复后复审与 production TODO/FIXME/HACK 扫描
- [ ] 全部 Release Gate 证据回填

最终目标：`Remaining platform defects = 0`。仅真实设备、现场网络/证书、真实地图/速度/量程、RPO/RTO 和 License owner decision 可留作外部输入。
