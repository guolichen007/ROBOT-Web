# G1 生产接入准备包：R001 firebot-vehicle-01 只读 Agent

> 状态：**操作合同与隔离验证证据**。本轮（G1-R1）未执行任何生产操作
> （`PRODUCTION_MUTATION=NONE`）；全部验证在隔离环境完成。
> 执行前置：G1-R1 全部 CI 绿 + OWNER 的 GO/NO-GO 授权。

## 1. 数据库

### 1.1 独立逻辑备份（执行当天，变更前）

```bash
# 受限目录（0700），独立于应用备份体系，不写入任何生产密钥
pg_dump -h <生产DB> -U <backup_user> -d firebot -Fc -f /opt/firebot/backup/r001-cutover-$(date +%Y%m%d).dump
```

### 1.2 一致性验证与隔离恢复证明（G1-R1 已隔离实测）

- 恢复目标：独立 scratch 库（`firebot_restore_check`），绝不复用生产库名。
- 验证步骤（已在隔离环境实测通过，证据见下）：

```bash
pg_restore -d firebot_restore_check <dump>
psql -d <prod>  -tAc "SELECT count(*) FROM robots"   # 3
psql -d <scratch> -tAc "SELECT count(*) FROM robots" # 3（相等）
psql -d <prod>  -tAc "SELECT md5(string_agg(vehicle_id||':'||online_state,',' ORDER BY vehicle_id)) FROM robots"
psql -d <scratch> -tAc "同上"                          # 校验和一致
```

- **G1-R1 隔离实测**：dump 219850 字节 → restore OK → robots 行数 3=3 →
  校验和 `5f2c50ff257a23e742bd612b8765683c` 两边一致 → `DB_BACKUP_RESTORE_PLAN=VERIFIED`。
- 执行前后快照：变更前记录 `firebot-vehicle-01` 行不存在/为预登记基线 + R002 行
  （id/online_state/boot_id/updated_at）；变更后再次记录；两快照随验收证据归档。
- 失败恢复：任一步 FAIL → 停止后续步骤 → 按回滚方案执行 → 保留 dump 与快照待人工处置；
  绝不在失败后继续下一阶段。

## 2. MQTT

### 2.1 R001 独立账号与最小上行 ACL

- 账号：username=`firebot-vehicle-01`，独立随机密码（openssl rand -hex 32 级别），
  与 R002/旧 bridge 凭证完全隔离；密码文件只经受控渠道分发（见 §3 凭证分发）。
- ACL（最小上行，复用 `scripts/check_vehicle_agent_acl.py` 校验）：

```text
user firebot-vehicle-01
pattern read v1/robot/%u/cmd
pattern write v1/robot/%u/ack
pattern write v1/robot/%u/status
pattern write v1/robot/%u/telemetry
pattern write v1/robot/%u/availability
pattern write v1/robot/%u/event
```

- **%u pattern 语义（G1-GO0 修正，按真实 broker 行为实测）**：
  1. pattern 规则仅作用于其所在 `user` 段；不同 user 段的 pattern 互不渗透；
  2. **passwd 有账号但 ACL 无 user 段的认证用户默认全拒**（deny-all）；
  3. 订阅 ACL 的拒绝语义依 broker 版本不同：mosquitto 1.6 对拒绝的 SUBSCRIBE
     仍回 SUBACK granted、**拒绝延迟到发布侧执行**（订阅方收不到任何消息）；
     mosquitto 2.x 回 SUBACK 0x80 显式拒绝。生产镜像 eclipse-mosquitto:2.1.2 为
     2.x 语义；任何断言不得只依赖 SUBACK 码，须以「投递结果」为准；
  4. 八场景实测（`test_acl_eight_scenarios_broker_behavior`，真实 broker 行为断言）：
     写自己 topic=PUBACK+订阅方收到；写他人 topic=订阅方不投递；订阅自己 cmd=收到
     platform 发布的命令；订阅他人 cmd=绝不投递；无 ACL 段账号=写不投递；有凭证无
     DB 行=broker 授权（投递到订阅方，DB 不建行由 ingress 兜底）；未认证=CONNACK
     Not authorized；legacy（robot/%u）授权面不退化。
  **现有生产 ACL 策略无需变更**：生产模板本就按 user 段声明（每车一段），无跨段
  pattern 使用；本次仅修正文档声明，不改生产 ACL。

### 2.2 CA 信任路径与轮换方案（BOOTSTRAP_LOCAL_ONLY 治理）

- 现状：Bootstrap CA 带 `BOOTSTRAP_LOCAL_ONLY` 标记，**不得用于正式生产接入**。
- 治理顺序（正式接入前完成，逐条验收）：
  1. 运维签发正式 MQTT CA（server cert SAN 覆盖生产域名与 IP），
     为 R001 单独签发/启用，**不替换全局 CA**（R002 既有信任链保持原样）；
  2. broker 端：新增 listener 或按审批流程切换 R001 通道证书；变更前备份原 cert/key；
  3. 车端：R001 Agent 身份配置 `ca_cert_path` 指向正式 CA（经 §3 凭证分发）；
  4. 平台侧 adapter/ingress 的 `MQTT_CA_FILE` 按审批节奏滚动重启（先车端后平台侧观察窗）。
- 证书更新方案：CA 轮换时新 CA + 新 server 证书 → broker reload → 客户端（车端/平台）
  信任文件先行更新 → 观察旧信任被拒（fail-closed）→ 移除旧 CA。**绝不静默降级
  insecure**。
- 隔离实测：`test_ca_rotation_isolated_old_trust_rejected`（E2E）：
  新 CA 客户端连新 broker 成功；旧 CA 客户端连新 broker **被拒**；旧 CA 客户端连旧
  broker 依旧可用（R002 式既有信任不受影响）→ `CA_MIGRATION_PLAN=VERIFIED`。

## 3. 安装（批准 SHA 完整产物）

- 按《Vehicle-Agent-V1只读Agent安装与回滚方案-G0A2.md》（G1-R1 修订版）执行：
  批准 SHA 仓库检出 → venv（Python 3.11 + `requirements.txt` 锁定）→ systemd 模板 →
  两阶段 preflight（preinstall/prestart）→ 启动。
- 凭证分发（铁律）：生产密码只存在于 `/etc/firebot/vehicle-agent-v1-readonly/
  device_identity.json`（root:firebot **0640**，Agent 只读不可写、其他用户不可读）；
  经运维安全通道分发，**不经命令行参数、环境文件、Git、日志**。
  隔离实测（systemd 验收 14/14）：非 root 服务用户可读、nobody 不可读、Agent 不可写。
- 日志轮转：systemd 单元输出走 journald（JournalMaxFileSize 默认策略）；
  `host-metrics.jsonl` 由 logrotate 兜底（建议 7 天/rotate 4，压缩），安装时由运维
  按现场策略配置；日志一律不含凭证（Agent 不打印密码，见脱敏日志样例）。
- 故障回滚：见 SOP §6（stop 新 Agent → 恢复旧链路 → 保留归档证据）。

## 4. 验证（G1 执行顺序，全部通过才继续下一步）

1. R001 先上行：启动 Agent → 云端 `firebot-vehicle-01` 显示真实 **ONLINE**、
   Agent 状态（`programs.agent=true`、其余 false）与**最后采集时间**（status 3s 周期、
   8s stale 门限内新鲜）。
2. 无任何硬件输出：`/cmd_vel`、Modbus/CAN/GPIO/继电器零引用（源码反证 + Mosquitto
   日志零 SUBSCRIBE 反证 + 结构上 `transport.subscribe` 即抛错）。
3. 无命令订阅：Mosquitto 日志无来自 `firebot-vehicle-01-readonly-v1` 的 SUBSCRIBE。
4. 下行总开关始终关闭：`VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false`、allowlist 空；
   该车能力列表 `supported_commands_json=[]`（注册基线即如此，绑定亦按下行开关诚实声明）。
5. 急停显示"未验证"（estop_active=SQL NULL），前端不显示"待命/安全待命"
   （只读监测语义），电池/位姿/传感器显示"未接入/无数据"。
6. 每一验证项只记录 PASS/FAIL 与时间，凭证/ACL 全文不进验收记录。

## 5. R002 三阶段独立观察（正式变更前/期间/结束后）

- 变更前：记录 R002 在线连接状态、boot_id、上报新鲜度（最近 N 条 server_received_at
  间隔）与近 24h 日志基线。
- 期间：连续观察 R002 连接不中断（0 次重连/心跳无异常波动）、boot_id 不变、
  新鲜度分布不劣化；任何异常 → 立即停止变更并按回滚执行。
- 结束后：三阶段数据对比一致 → `R002_UNCHANGED=YES`；不一致 → 人工处置并如实上报。
- 隔离侧证据：真实链路 E2E 中 R002 快照比对零变更（`test_cross_device_topic_denied_and
  r002_untouched`），且本轮全部代码 diff 零 R002 相关文件。

## 6. GO/NO-GO 清单

```text
DB 备份+恢复验证    VERIFIED（隔离实测）
CA 轮换方案         VERIFIED（隔离实测：旧信任被拒、新信任可用、R002 链路不受影响）
ACL %u 语义         VERIFIED（无 user 段=全拒；pattern 段内生效）
systemd 安装        14/14 隔离验收通过（非 root 读取/不可写/WorkingDirectory/重启/退出码）
HMI 语义            前端 100/100（含 001 只读 + R002 既有回归）
注册流程            真实 DB 4/4（唯一身份/幂等/审计/estop 未验证/能力诚实）
凭证泄漏            NONE（fleet-register 改 stdin 注入；合同测试锁定）
```
