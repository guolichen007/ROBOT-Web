# vehicle-agent-v1-readonly（G0-A2 真实只读 Agent）

Firebot R001 车端只读 Agent。运行在真实工控机上，只采集真实 OS 与进程信息，并按
`services/vehicle_agent_v1.py` 合同上行 `availability` / `status` / `telemetry`。
**不订阅任何命令、不产生任何硬件输出。**

## 版本与角色

| 项 | 值 |
| --- | --- |
| 名称 | `vehicle-agent-v1-readonly` |
| 版本 | `0.1.0`（`AGENT_VERSION`，`__init__.py`） |
| 角色 | `readonly-telemetry`（只读遥测，无控制能力） |
| 目标设备 | canonical ID `firebot-vehicle-01`，云端显示名 `firebot-001` |
| Python | ≥3.11（合同模块 `services/vehicle_agent_v1.py` 使用 `datetime.UTC`） |
| 依赖 | `paho-mqtt==2.1.0`、`pydantic==2.11.7`（与仓库 `requirements.txt` 一致） |

## 运行方式（本地隔离验收）

```bash
# 仓库根目录，虚拟环境已安装依赖
PYTHONPATH=. python3 -m services.vehicle_agent_v1_readonly.main \
  --config services/vehicle_agent_v1_readonly/config/device_identity.firebot-vehicle-01.acceptance.example.json \
  --run-dir /tmp/firebot-vehicle-01-run \
  --status-interval 5 --telemetry-interval 15 \
  --allow-placeholder-credentials
```

- `--allow-placeholder-credentials`：仅本地隔离验收可用；G1 正式凭证不得含占位标记。
- 退出码：`0` 正常退出；`2` 配置错误；`3` owner 冲突（旧 Bridge 运行 / 本 Agent 双开）；`4` 连续连接失败放弃。
- 优雅退出：SIGTERM/SIGINT → 发布 retained `offline` → 干净断开 → 退出 `0`。

## 上行合同（完全复用 `services/vehicle_agent_v1.py`）

| 消息 | topic | QoS | retained | payload |
| --- | --- | --- | --- | --- |
| availability | `v1/robot/{vehicle_id}/availability` | 1 | true | `"online"` / `"offline"`（字符串，同 mock 语义；建连时 retained 一次） |
| availability(live) | 同上 | 1 | **false** | 周期性 `"online"`（默认 5s，非 retained）：云端 Adapter 中断恢复后无需车辆重连即可解除清扫器 offline 压制并恢复 ONLINE；不触发 retained 安全守卫、不污染 broker 存储的 retained 值 |
| status | `v1/robot/{vehicle_id}/status` | 1 | true | `{"online": true, "programs": {...}, "task": {...}, "ts": ISO}` |
| telemetry | `v1/robot/{vehicle_id}/telemetry` | 0 | false | `{"pose": null, "battery": null, "smog": null, "thermal": null, "ts": ISO}` |

- `programs`：`agent=true`（Agent 自身存活）+ 平台已知程序键（chassis/navigation/patrol/
  thermal/battery/smog/camera）；001 未接入时对应进程不存在 → `false`，绝不伪造。
- `telemetry` 四个硬件字段显式 `null`：未接入硬件的空值语义，不生成模拟数值。
- 时序（G1-R0 A1）：status 默认 **3s**（适配云端 8s stale 门限），telemetry 默认 30s；
  CLI 对负数/0/NaN/低于 1s 的间隔直接拒绝（退出码 2）。
- 确认（G1-R0 A2）：QoS1 发布等待 broker PUBACK，未确认只在日志记 `UNCONFIRMED`，
  绝不宣称送达；在途/排队上限（8/64）与退避封顶（30s）保证重试资源有界。
- 断线：paho 有界重连 + 指数退避（1s→30s，含抖动）；异常断线由 broker 发布 LWT
  `offline`（retained）；正常退出先发布 `offline` 再干净断开。
- 重复 status：retained 语义下 broker 仅保留最新一条，重复发布幂等；平台侧按
  event_id 去重（`vehicle_agent_ingress.process`，TTL 300s）。
- host 指标（OS 版本/内核/uptime/负载/内存/磁盘/Agent 运行状态）默认只写本地
  `host-metrics.jsonl`；上行属实验性扩展（`--host-metrics status-field`，默认关闭，
  平台 `UplinkModel extra=ignore` 保证兼容）。见
  `docs/技术合同/VEHICLE_AGENT_V1_HOST_METRICS_EXTENSION.md`。

## 安全边界（本轮硬性保证）

- **身份**：只读 `device_identity.json`（单一配置源）。环境存在 `FIREBOT_VEHICLE_ID`
  / `BRIDGE_VEHICLE_ID` 或配置文件名为 `bridge.env` → 拒绝启动。`R001`/`R002`/
  `firebot-vehicle-02` 在配置层直接拒绝。`mqtt_username` 必须等于 `vehicle_id`
  （broker ACL `%u` 合同）。
- **TLS**：`cert_reqs=CERT_REQUIRED`，只信任配置 CA，绝不调用 `tls_insecure_set`。
- **无控制**：不订阅 `cmd`；`MqttTransport.subscribe` 结构上抛错；不实现任何命令处理；
  不发布 `/cmd_vel`；不写 Modbus/CAN/GPIO/继电器；不导入 ROS。
- **单 owner**：检测到旧 `vehicle-bridge` 进程 → 拒绝启动；本 Agent 双开 → flock 拒绝
  （SIGKILL 后内核自动释放锁，可再次启动）；**运行期**检测到旧 Bridge 被重启 →
  发布 offline 并退出码 3（fail-closed），systemd Restart=on-failure 接手后由启动
  守卫持续拒绝，直至冲突解除。
- **凭证权限（G1-R1）**：正式方案 root:firebot + **0640**（服务用户组内可读、
  Agent 自身不可写、其他普通用户不可读）；Agent 启动时强校验"禁止组写 + 禁止
  其他任意位"。安装前执行两阶段 preflight（`preinstall` 环境预检 / `prestart`
  产物与运行态预检，lock 以真实 flock 获取验证）。
- **不触碰**：本轮不安装、不启动生产服务、不修改工控机系统文件（见 G1 方案文档）。

## 脱敏日志样例（本地验收 broker，身份为本地测试占位）

```text
2026-11-11 13:20:01 INFO vehicle-agent-v1-readonly vehicle-agent-v1-readonly 0.1.0 starting as firebot-vehicle-01 (display firebot-001); broker 127.0.0.1:8883
2026-11-11 13:20:01 INFO vehicle-agent-v1-readonly safety: no subscription, no hardware output, no velocity command, no bridge.env
2026-11-11 13:20:02 INFO vehicle-agent-v1-readonly connected as firebot-vehicle-01; availability=online published
2026-11-11 13:20:07 INFO vehicle-agent-v1-readonly signal 15 received; shutting down gracefully
2026-11-11 13:20:07 INFO vehicle-agent-v1-readonly graceful offline published; agent stopped
```

（凭证、密码、CA 私钥一律不出现在日志中。）

## 测试

```bash
# 仓库根目录
PYTHONPATH=. pytest services/protocol-tester/tests/test_vehicle_agent_v1_readonly.py -v
```

测试全部使用本地进程内 FakeBroker（TLS、CONNACK、LWT、retained、QoS 全实现），
**不连接任何真实生产 Broker**。测试证书为 TEST ONLY：由 `scripts/generate_readonly_test_certs.sh` 在测试会话开始时现场生成（仓库不提交任何 .key/.crt）。
