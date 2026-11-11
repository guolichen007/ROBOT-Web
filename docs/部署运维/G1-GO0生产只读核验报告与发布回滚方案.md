# G1-GO0 生产只读核验报告与发布回滚方案

> 状态：**只读核验 + 方案文档**。本轮未修改生产服务器任何文件/进程（`PRODUCTION_MUTATION=NONE`）。
> 所有可远程只读核验的项目已执行；需服务器侧权限的项目给出操作员核验清单，如实标注
> 「本机无法核实」，**绝不把 CI 测试库结论冒充生产结论**。

## 1. 公网 MQTT TLS 只读核验（已执行，脱敏）

| 项 | 实测值（2026-11-11） |
| --- | --- |
| 目标 | `firebot.youdaozd.com:8883`（124.223.220.182，公网可达） |
| Subject | `CN=mosquitto` |
| **Issuer** | **`CN=U-Dao Bootstrap CA`** |
| SAN | `DNS:mosquitto, DNS:firebot.youdaozd.com, DNS:localhost, DNS:u-dao, IP:127.0.0.1` |
| 有效期 | 2026-10-27 → 2029-01-29 |
| SHA256 指纹 | `20:88:DD:BD:30:35:F2:DB:95:E8:E7:C3:AD:F9:3F:D8:6C:AC:D4:C7:E4:C6:5A:1E:CE:7F:2F:9E:9E:40:8C:A7` |
| 主机名验证 | SAN 含 `firebot.youdaozd.com` → 机械上可验证，**但信任锚是 Bootstrap CA** |

- **CA 来源与标记**：Issuer 与 `deploy/u-dao/scripts/02-gen-bootstrap-secrets.sh` 生成的
  `CN=U-Dao Bootstrap CA` 一致；该脚本第 4 行明确 **BOOTSTRAP_LOCAL_ONLY：非公网证书，
  真实车辆接入前必须替换**。结论：**当前 8883 尚未完成正式 CA 治理**。
- **R002 信任路径**：车端 `FIREBOT_CA_CERT=/etc/firebot/mqtt-ca.crt`（firebotctl:209），
  即 R002 现网信任链指向 Bootstrap CA。任何更换 8883 证书 CA 的动作都会破坏 R002 既有
  信任，除非 R002 车端信任文件先更新——**R002 全面冻结，不可触碰**。
- **零停机可行性结论（`CA_ZERO_DOWNTIME_PLAN=BLOCKED`）**：
  1. 新增独立 listener（如 8884）需要 broker restart（mosquitto listener 不可 SIGHUP
     热增）→ 影响 R002 连接 → 无授权停机窗口内不可行；
  2. 现有 listener 换证（cert/key 文件替换 + SIGHUP）能否热生效取决于 mosquitto 2.1.2
     现网实测；且新证书若非 Bootstrap CA 签发，R002 车端（仅信 Bootstrap CA）立即验证
     失败——违反 R002 冻结；
  3. 本机无法在现网验证 SIGHUP 换证行为；不得用两个隔离 broker 的测试结果代替现网证明。
  → 按任务书要求明确输出 **`CA_ZERO_DOWNTIME_PLAN=BLOCKED`**，并提交独立接入方案（下节）。

### 1.1 独立接入方案（提交审批，本轮不执行）

1. 运维签发正式 MQTT CA（非 bootstrap，公网 SAN 覆盖 `firebot.youdaozd.com`）；
2. 新 server 证书由正式 CA 签发，并经 **Bootstrap CA 交叉签名**（cross-sign）：
   server 端返回的证书链同时终结于正式 CA（R001 新信任）与 Bootstrap CA（R002 旧信任），
   **R002 车端零改动**；
3. 受控窗口内先验证 mosquitto 2.1.2 的 SIGHUP 证书热重载行为（用独立测试客户端观察，
   不触碰 R002 会话），通过后执行「替换 cert/key → SIGHUP → 三阶段观察 R002」；
4. 若第 3 步验证失败 → 回退方案：维护窗口（OWNER 授权）新增 8884 listener + restart，
   R002 影响窗口与回滚步骤预先书面化；本轮不执行任何变更。

## 2. 数据库与运行版本只读核对

| 项 | 本机可核实性 | 结论/操作 |
| --- | --- | --- |
| 生产 Robot 总数 / synthetic R001 原始行 / firebot-vehicle-01 不存在 / R002 实时在线 | **本机无法核实**（无生产服务器访问权限） | 运维窗口执行下方只读 SQL 清单；如实记录，不推断 |
| 服务器运行 SHA / API、Web 镜像来源 | **本机无法核实** | 运维窗口 `server-deploy.sh status` + `docker inspect` 记录 |
| 生产 DB 真实备份是否缺失 | **本机无法核实** → 当前仅能标注 `PROD_DB_BACKUP=UNVERIFIED（待窗口核实）` | 严格区分：G1-R1 已验证的是 **CI 隔离测试库** 的 dump/restore（3=3 行、校验和一致），**不是生产备份** |
| 备份工具在受控窗口创建并隔离恢复的能力 | 方案已备（见 G1 准备包 §1），生产执行待窗口 | 与生产备份同样不得提前宣称 VERIFIED |

运维窗口只读 SQL 清单（脱敏，禁止写操作）：

```sql
SELECT count(*) FROM robots;                                   -- Robot 总数
SELECT vehicle_id, agent_protocol, online_state, enabled,
       estop_active, boot_id, updated_at
  FROM robots WHERE vehicle_id IN ('R001','firebot-vehicle-02');  -- R001 原始行 + R002 基线
SELECT count(*) FROM robots WHERE vehicle_id = 'firebot-vehicle-01';  -- 期望 0（身份无冲突）
```

## 3. 发布与回滚：最小变更顺序（8 步）

| 步 | 动作 | 执行者 | STOP 条件 | 回滚范围 | 不可自动回滚 |
| --- | --- | --- | --- | --- | --- |
| 1 | Firebot DB 真实备份 + 隔离恢复验证 | 运维 | 备份失败/恢复校验和不一致 → 停止 | 无变更 | — |
| 2 | R001 独立凭证、ACL、证书路径（含 §1.1 方案） | 运维 | 任何 FAIL/影响 R002 → 停止 | 删除新增账号/ACL 段 | CA/证书变更需按 §1.1 回退步骤 |
| 3 | 生产 API/Web 代码与镜像差异核对（批准 SHA vs 运行态） | 运维+交付 | 差异未解释 → 停止 | 不部署 | — |
| 4 | 新建 firebot-vehicle-01 唯一 Robot（fleet preregister 路径） | 运维 | 已存在/冲突 → 停止 | 删除新行 | 审计日志保留 |
| 5 | 受控 V1 协议绑定（API，enabled=false、能力空、estop 未验证） | 运维 | 绑定失败/审计缺失 → 停止 | 协议回绑 CANONICAL_MQTT | 审计日志保留 |
| 6 | 001 安装批准 SHA 只读 Agent（两阶段 preflight + systemd 模板） | 现场工程师 | preflight FAIL → 停止 | SOP §6 回滚路径 | 车端物理变更需现场处置 |
| 7 | 验证真实上行至 Web（ONLINE/时间戳/无订阅/无硬件输出/下行关） | 交付+运维 | 任一验证 FAIL → 回滚到第 5 步前状态 | 停 Agent → 离线 | 线上历史状态由平台语义接管 |
| 8 | 观察 R002 与异常回滚（三阶段观察） | 运维 | R002 任何异常 → 立即回滚 | 按 SOP §6 + 第 2 步反向操作 | 已写入的审计/任务记录保留 |

每步完成后记录 PASS/FAIL 与时间戳；凭证、ACL 全文、连接串不得进入任何记录。
