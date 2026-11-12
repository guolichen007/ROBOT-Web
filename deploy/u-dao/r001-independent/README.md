# R001 独立接入部署模板（G1-P2-RC 版本化工件）

> 本目录为**脱敏模板**：全部敏感值经 `--env-file` 注入（宿主 root:600），
> 禁止提交测试密钥、口令或真实生产配置。镜像 SHA 以部署记录 manifest 为准。

## 1. 工件与版本

| 项 | 值 |
| --- | --- |
| 模板来源 SHA | 随仓库版本（本模板所在 commit） |
| 运行镜像 | `FIREBOT_PYTHON_IMAGE`（默认 firebot-python:server，生产以批准 SHA 构建/标签，manifest 记录 image_id+revision） |
| Broker 镜像 | eclipse-mosquitto:2.1.2-alpine（与生产同版） |
| 生产 Broker | **零变更**（不 SIGHUP/不重建/不改 ACL/凭证） |

## 2. 前置（全部需 OWNER 授权，未授权不得执行）

1. 生产 Firebot DB 备份 + 隔离恢复演练通过。
2. 独立入口公网端口（默认 8884）与腾讯云安全组放行。
3. R001 车辆资产已按受控路径 preregister（enabled=false）+ 协议绑定 VEHICLE_AGENT_V1。
4. 云端 API/Web 升级至含 G1-P2 修复的批准 SHA（无迁移；回滚=旧镜像 + 无 DB 回滚需求）。

## 3. 独立 Broker 证书与凭证（正式公网 TLS 主机名验证）

- 签发 R001 专属 CA 与服务器证书：**SAN 必须包含正式接入主机名**（如
  `r001-mqtt.youdaozd.com`），车辆 Agent 只信任该 CA（`ca_cert_path`），
  主机名验证由 Agent TLS 层强制执行（错误 CA/错误主机名一律拒绝，无降级）。
- 凭证生成：mosquitto_passwd 为 `platform`（本 Broker 专属，独立生成，
  **绝不复用生产 platform 口令**）与 `firebot-vehicle-01`。
- ACL：与生产 %u 模板一致（平台段 readwrite `_platform/vehicle-agent-v1/+/+` 与
  `v1/robot/+/+`；车辆段仅自车上行 + 自车 cmd 读）。
- `r001.env`（root:600，不入库）示例键：
  `R001_BROKER_CONF_DIR`、`R001_BROKER_PLATFORM_PASSWORD`、
  `R001_SCOPE_VEHICLE_IDS=firebot-vehicle-01`、`R001_MQTT_PUBLIC_BIND=0.0.0.0:8884:8883`、
  `FIREBOT_PYTHON_IMAGE=<批准 SHA 镜像>`。

## 4. 启动 / 停止 / 回滚

```bash
# 启动（--wait 等 healthcheck；任何 unhealthy 即 STOP 排障，不进入验收）
docker compose -f compose.r001-independent.yml --env-file r001.env up -d --wait
# 停止（生产栈不受影响；车辆上行停更）
docker compose -f compose.r001-independent.yml --env-file r001.env down
# 回滚 = down（本栈无业务状态卷；历史数据不删除；如需彻底清理再删 r001_mqtt_data 卷）
```

## 5. 验收清单（G1-P2 生产放行时逐项执行）

- R001 Agent 上行：Web 车辆列表可选中（enabled=false V1 只读可监控）→ 监控页
  ONLINE/链路实时（status 驱动）、硬件字段各自 freshness、急停"未验证"。
- 专属实例心跳键：`service:{mqtt-ingress,vehicle-agent-adapter}:heartbeat:r001-dedicated-*`
  与生产实例键并存；生产实例 healthcheck 不受影响。
- 范围白名单：非 `firebot-vehicle-01` 的车辆消息在独立 Broker 上被丢弃（不落库）。
- 下行 fail-closed：`VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false` + allowlist 空。
- R002 零影响：R002 上线状态下对比观察（连接/ACL/容器 StartedAt/媒体端口计数不变）。

## 6. 最小 API/Web 升级方案（与独立入口配套）

- API：G1-R1 两处修复（preregister/bind_protocol 急停 SQL NULL；下行关闭时
  能力列表诚实置空）+ G1-P2 链路 freshness（`server_received_at` 每条服务端确认
  消息即刷新，不再只随 telemetry 更新）。
- Web：G1-R1 只读监测/estop 未验证 HMI + G1-P2 车辆列表对 enabled=false 的
  VEHICLE_AGENT_V1 设备开放"切换监控"入口（后端控制门不变）。
- 无 alembic 迁移；回滚边界 = 旧镜像标签（firebot-web/firebot-python @ 原 SHA）。

## 附：G1-P2-RC 本地真实共存实测记录（staging，非生产）

- 双实例共享同一 PG/Redis：原栈 Adapter/Ingress + 专属 Adapter/Ingress
  （FIREBOT_SERVICE_INSTANCE_ID=r001-dedicated-*，scope=firebot-vehicle-01）同时运行：
  6 个心跳键并存（兼容键 ×2 + 命名空间键 ×4），healthcheck 各自实例互不干扰。
- 独立 Broker2（独立证书 SAN=r001-broker2，主机名验证生效）承载 R001 Agent 上行，
  经专属实例落库 ONLINE。
- 范围过滤实证：scope 外车辆（firebot-vehicle-77，有云端资产+ACL 段）在 Broker2 发布
  v1 status → 专属 Adapter 丢弃 → 不落库（Redis latest 不存在）。
- 原实例兼容心跳持续更新，原链路不受影响。

## 附：G1-P2-RC1 实测修正（模板文件权限与挂载约定）

- 服务容器以 uid 10001 运行：`r001-platform-pw`（专属 broker platform 口令）与 CA 证书
  宿主文件须对该 uid 可读（如 644/或 chown 10001）；broker 所需文件（passwords/acl/
  server.key）属 1883。
- 口令文件挂载点必须避开生产 app_secrets 卷的只读挂载目录：使用 `/run/r001-secrets/`
  独立路径（见 compose 注释）。
- Broker healthcheck 口令经容器内 secret 文件运行时读取（`$$(cat ...)`），不进入容器配置元数据。
- G1-P2-RC1 实测：`CREDENTIAL_IN_DOCKER_METADATA=NONE`（模板三容器 inspect 无任何口令值）。
