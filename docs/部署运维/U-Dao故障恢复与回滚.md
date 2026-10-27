# U-Dao 故障恢复与回滚

> 单服务器不能宣称物理「零中断」：本架构目标是**自动恢复 / 故障收敛**，且故障期间不出现：
> 重复执行、旧命令复活、manual 补发、ACK 错配、状态假成功。

## 1. 恢复模型总则

- **MQTT 短暂断连**：靠 paho 有界重连（`reconnect_delay_set(1s,30s)`）在**原容器内**恢复；healthcheck（Redis 心跳绑定 `is_connected()`）只**暴露** unhealthy，不依赖「unhealthy → Docker 重启」假设；`restart: unless-stopped` 只兜进程 crash/exit。
- **broker 恢复**：mosquitto `persistence true`；durable 客户端（ingress-durable / vehicle-agent-adapter）固定 client id + Session Expiry 600s → 短时断连后恢复订阅并接收 broker 代存的 QoS1 业务事件（command_ack/task_status/alarm/event）；realtime 客户端 clean session → 高频遥测（location/heartbeat/sensor/status/availability）**不做离线 backlog**，恢复后直接收新鲜数据（无「先回放历史位置再收敛」）。
- **过期命令不复活**：三层——发布前 `expires_at<=now` 直接不发布；MQTT5 Message Expiry Interval `max(1,ceil(remaining))`（broker 队列中到期即弃）；车辆仍必须校验 payload `expires_at`（第二层只是兜底）。task-worker 对 QUEUED 滞留过期命令置 EXPIRED。
- **manual 不补发**：QoS0 + Redis pub/sub 无缓冲（dispatcher 宕机即丢脉冲）+ broker `queue_qos0_messages false` + 500ms TTL + 车端本地 deadman/watchdog；云端不能替代车辆本地安全停止。
- **WebSocket 恢复**：客户端无界指数退避（500ms×2^n，30s 封顶，±20% 抖动）+ 服务端 `resync_required`(4009) 兜底全量重同步；API restart 后自动恢复。
- **媒体恢复**：media-test-source（合成）测完即停，非正式后台服务；mediamtx restart 后需重新拉起合成发布器（验收流程 `compose.media-e2e up`）；VideoCard 有界自动重连（3 次，1/2/4s 退避，epoch 防污染，仅用户已建立会话且流仍 active 时）。
- **shared ingress**：变量式 proxy_pass + Docker DNS 运行时解析——firebot edge 重建后按 TTL 自动恢复，无人工 reload（兜底安全 reload 属 platform 维护，Firebot 脚本不得代执行）。

## 2. 故障注入验收（`scripts/cloud_fault_injection.sh`）

| 用例 | 场景 | 断言 |
|---|---|---|
| A1 | restart mqtt-ingress | 心跳恢复、订阅恢复 |
| A2 | restart vehicle-agent-adapter（启用时） | 心跳恢复 |
| A3 | restart mosquitto | ingress/dispatcher 心跳恢复且**容器未重建**（StartedAt 不变） |
| A4 | restart api | /health/ready 恢复 |
| A5 | restart redis | 各服务心跳恢复 |
| A6 | restart mediamtx | healthy 恢复 |
| A7 | mosquitto pause 10s | 心跳恢复、容器未重建 |
| A8 | stop ingress 期间 flood 50 条 location + 1 条 alarm | durable 队列投递 alarm（dedup 键存在）；realtime **无**历史 location 回放（旧 dedup 键不存在）；恢复后最新 location 直达收敛（seq=51） |
| B1 | 离线持久会话订阅者 + Message Expiry 2s 的消息 | 重连后**不**收到（过期即弃） |
| B2 | 离线订阅者 + QoS0 消息 | 重连后**不**收到（manual 脉冲语义） |
| B3 | 对照：离线持久订阅者 + 未过期 QoS1 | 重连后**收到**（durable 恢复成立） |
| C1 | firebot edge force-recreate | 无人工 reload，ingress 18000/18443 health 恢复 |

## 3. 版本回滚

- `TARGET_SHA=<任意40SHA> server-deploy.sh rollback`：
  - `previous=none` → 拒绝（fresh 首发布无回滚目标，禁止伪造）。
  - U-Dao 守卫：回滚目标必须含 `deploy/u-dao/compose.u-dao.yml`。
  - **schema gate**：回滚目标 migration head ≠ 当前 DB `alembic_version` → 拒绝（不伪造 PASS；DB 永不 downgrade）。
  - 执行 = 切换 `FIREBOT_RELEASE_ROOT` 到 `releases/<prev>` 并 recreate（**绝不在 immutable release 内 checkout**；release 断言：目录名==HEAD==SHA、clean、detached）。
- 首发布失败：`AUTO_ROLLBACK=FAIL（fresh 首发布无回滚目标）`——清理失败 runtime 后重新 bootstrap，不伪造 previous。
- 第二次发布失败：自动回滚到 previous SHA（release 切换）。
- 每次回滚写 manifest（rollback 类型，记录 before/after revision 与第三方镜像 RepoDigest）。

## 4. 数据库备份与验证

- `backup`：pg_dump|gzip → `runtime/deployments/backups/firebot-<ts>.sql.gz`（0600 + sha256 manifest）。
- `backup-verify <file>`：隔离临时库 `firebot_backup_verify` 内 restore + 基础一致性（public 表数 ≥10、robots/users/commands 计数可查）→ drop。**仅 dump 存在不算 VERIFIED**。
- 升级前：backup → backup-verify → 人工 `BACKUP_VERIFIED=1` → `migrate`。
- 同盘备份 ≠ 灾难恢复：COS/第二云盘的 off-host 备份为 OWNER_ACTION_REQUIRED。

## 5. 应急排查（速查）

```bash
# 服务状态 / 部署状态
sudo -n docker compose -p u-dao-firebot --env-file /srv/u-dao/projects/firebot/runtime/env/.env.server \
  -f <release>/docker-compose.server.yml -f <release>/deploy/u-dao/compose.u-dao.yml ps
TARGET_SHA=<sha> bash scripts/server-deploy.sh status    # current.json + 容器表
# MQTT 心跳（Redis，api 容器内）
sudo -n docker compose -p u-dao-firebot exec api python -c "from app.core.events import get_redis; print(get_redis().get('service:mqtt-ingress:heartbeat'))"
# 日志（json-file 20m×5 已限界）
sudo -n docker compose -p u-dao-firebot logs --tail=200 mqtt-ingress command-dispatcher
# 宿主端口复核（必须全部 127.0.0.1）
ss -lntup | grep -E '18000|18443|18080|18883|18554|8189'
# 挂载守卫复核
systemctl show docker | grep -E 'RequiresMountsFor|ConditionResult'
```

## 6. 已知剩余风险（如实记录）

- 单机单副本：control-plane 各服务单实例（manual 多副本会经 Redis pubsub 重复发布脉冲，V1 约束单副本；`FIREBOT_SERVICE_INSTANCE_ID` 已为未来副本化预留）。
- 公网 ICE/TURN 未验证：8189 公网暴露与 TURN 部署为 OWNER_ACTION；本阶段仅 localhost candidate 断言。
- 监控/告警：platform/monitoring 仅目录骨架，未引入监控栈（避免过度工程化；owner 后续决定）。
- `PUBLIC_PRODUCTION_READY=NO`：域名/安全组/正式 TLS/公网 SRT-WebRTC/off-host 备份/真实车凭据未完成前，不得宣称生产就绪。
