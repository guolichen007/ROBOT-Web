# vehicle-agent 真实组件离线 E2E（G1-R0）

真实链路验收：**真实 Mosquitto（TLS + per-device 凭证 + 生产同款 ACL pattern）→
现有 vehicle-agent-adapter → mqtt-ingress → 真实 PostgreSQL/Redis → 现有 FastAPI**。
所有组件只连 127.0.0.1 测试端口，禁止连接生产 Broker；测试资产（`firebot-vehicle-01`）
与凭证均为测试专属，不复用 R001/R002 历史数据。

## 运行前提（本机 / CI 专用 job）

1. `REAL_E2E=1`（缺少时全部 skip，防止在普通 job 里误跑真实组件）；
2. PostgreSQL + Redis 就绪且已执行 `alembic upgrade head && python -m app.db.seed`
   （与 backend job 相同步骤，seed 提供 site/admin/R001/R002 基线）；
3. `mosquitto` + `mosquitto_passwd`（密码文件生成）与 `openssl`（测试证书）可执行；
4. 完整依赖：`pip install -r requirements-dev.txt`。

环境变量（conftest 提供默认测试值，CI 只需覆盖 DATABASE_URL/REDIS_URL/密钥）：

```bash
REAL_E2E=1 \
pytest integration/vehicle-agent-real-e2e -v
```

## 覆盖清单（对应任务书 B 的 9 项）

| 项 | 测试 |
| --- | --- |
| 1 真实 Agent 上报 availability/status/telemetry | test_real_agent_flow_reaches_api_online |
| 2 Adapter 正确规范化并转发 | 同上（API 状态来自 `_platform/vehicle-agent-v1` 内部 topic 转发） |
| 3 Ingress 更新正式 V1 Robot 状态 | 同上（DB 行断言） |
| 4 API 返回 ONLINE、真实时间戳与 Agent 状态 | 同上（last_seen_at 新鲜度 + agent_state_json） |
| 5 重连/LWT/正常退出 ONLINE↔OFFLINE | test_graceful_sigterm_confirmed_offline、test_crash_lwt_offline_then_restart_online |
| 6 假凭证/错误 CA/跨设备 topic/未知设备全部拒绝 | test_wrong_credentials…、test_wrong_ca…、test_cross_device…、test_unknown_device… |
| 7 未接底盘不产生假电池/假位姿/假就绪 | test_real_agent_flow…、test_no_fake_hardware_and_control_gate |
| 8 Agent 不订阅 cmd、downlink 关闭 | test_no_fake_hardware_and_control_gate（Mosquitto 日志 SUBSCRIBE 反证） |
| 9 R002 相关测试不退化 | test_cross_device_topic_denied_and_r002_untouched（会话级快照比对）；既有 R002 测试由 backend job 继续全量执行 |

## 清理

会话结束自动终止 adapter/ingress/mosquitto 子进程；测试文件（passwd/acl/证书）
全部位于 pytest 临时目录，无任何生产副作用。
