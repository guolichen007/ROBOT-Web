# 变更日志

## Repository consolidation / current sealed state

- SERVER_RUNTIME_BASELINE= `584efcfda60b8d39c516a9939bc0481609fc6f3c`（不可变 tag `baseline/server-runtime-2026-09-03`）
- DB= `20260902_0008`
- SERVER_FINAL_SEAL=PASS
- SAMPLE2=OFFLINE
- SAMPLE2_FINAL_ACCEPTANCE=PENDING
- FINAL_RELEASE=HOLD
- 长期主线 `main`；文档收敛为唯一索引 docs/README.md + 四目录（当前状态/部署运维/技术合同/开发验收）。
- CI 收敛到 main 主线（feature/fix/maintenance/field/release）；alarm lifecycle 测试对齐生产状态机；server-verify 容器健康解析修复。

## Vehicle Bridge 现场交接收尾（Historical，旧基线）

> 以下为历史记录，`13c869247079b88da11b36666755906001a0041c` 与 `REAL_CONTROL=NOT_IMPLEMENTED` 均为旧基线表述，不代表当前封板状态。

- Vehicle Bridge runtime 冻结于 `13c869247079b88da11b36666755906001a0041c`（Field Console / ROS-MQTT 生命周期隔离 / 现场交接文档完成）。
- 新增现场文档：实车现场联调总览（SSOT）、车端Bridge部署与实车接口、服务器与Web现场配合。
- 归档历史 UI 报告、CODEX 基线与工程报告；修正车端唯一配置路径与 CA 处理说明。
- 当前状态：`FIELD_R0_R4=GO`、`REAL_CONTROL=NOT_IMPLEMENTED`（R0–R4 尚未执行，不声称 PASS）。

## 当前主线

- 将实时监控重构为低干扰工业 HMI，首页突出二维停车场地图、车顶视频、活动报警和四个主控制动作。
- 新增 54 车位停车场、右侧传感器安装外参与 polygon 覆盖计算、五态数据质量显示。
- 新增预设位置、巡检计划/安全调度、停止巡检静止确认、巡检报告（Web/PDF/Excel）和三种灭火处置业务。
- 新增只读 ROS 原生兼容 Adapter、显式设备绑定、通道溯源及可配置 STALE/OFFLINE 阈值；Canonical MQTT 继续冻结为 Schema 1.2。
- 修复 safety dispatcher 消费组缺失时线程退出且 readiness 假健康的问题，outbox 与 safety 子循环改为独立心跳。

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
