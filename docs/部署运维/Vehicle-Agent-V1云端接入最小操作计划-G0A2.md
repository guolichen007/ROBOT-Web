# Vehicle-Agent-V1 云端接入最小操作计划（G0-A2 交付，G1 执行）

> 现状：云端目前**没有** `firebot-vehicle-01` 的正式数据库记录。
> 本计划只描述最小接入步骤；本轮（G0-A2）不执行任何生产操作。

## 1. 目标

建立真实设备 `firebot-vehicle-01`（显示名 `firebot-001`）的云端记录与 per-device
MQTT 凭证，使其只读遥测可被平台消费；**不开下行（downlink=false）**。

## 2. 云端记录创建（G1 执行，参考 apps/api/app/dev/setup_vehicle_agent_mock.py 的创建形状）

- `vehicle_id`（canonical ID）：`firebot-vehicle-01`
- 显示名：`firebot-001`
- `agent_protocol`：`VEHICLE_AGENT_V1`
- 初始状态：OFFLINE（首次上电后由 availability/status 驱动为 ONLINE）
- 不创建任何任务/地图关联（无底盘/导航接入）
- 历史 synthetic `R001` 保持冻结；`firebot-vehicle-02`（R002）任何字段不得修改

## 3. per-device MQTT 凭证与 ACL（运维执行）

1. 新建 MQTT 账号：username = `firebot-vehicle-01`（ACL `%u` 合同），独立随机密码。
2. ACL 核对（复用 `scripts/check_vehicle_agent_acl.py` 验证模板与实际 ACL）：
   - `pattern read v1/robot/firebot-vehicle-01/cmd`（本阶段 Agent 不订阅，保留最小面）
   - `pattern write v1/robot/firebot-vehicle-01/{ack,status,telemetry,availability,event}`
3. 下发生产 broker CA 给车端；服务器侧按《首次上线与回滚SOP》保持
   `VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false`。

## 4. 车端上线（按《安装与回滚方案》执行）

1. 部署只读 Agent + 正式身份配置（禁止占位凭证）。
2. 启动后预期上行序列：
   - `availability=online`（QoS1 retained）→ 云端 ONLINE
   - 周期 `status`（QoS1 retained，~10s）：`programs.agent=true`，其余 false
   - 周期 `telemetry`（QoS0，~30s）：硬件字段显式 null（未接入语义）
3. 云端验收点：ONLINE 状态、status 新鲜度、数据通道记录（RobotDataChannel）、
   无异常事件；`host` 扩展字段**不应出现**（默认关闭）。

## 5. 异常与回滚

- 凭证错误/CONNACK 拒绝：Agent 指数退避重试（1s→30s），日志 `connect failed`；
  不产生任何上行数据。修复凭证后自动恢复。
- CA 错误：TLS 握手失败，同上重试，绝不降级 insecure。
- 需要撤离：车端 `systemctl stop` → retained offline → 云端 OFFLINE；
  云端记录保留（仅状态变化），无需删除记录。

## 6. 明确不做（本阶段）

- 不开 downlink、不发任何命令、不验证运动。
- 不改动 `firebot-vehicle-02` 的任何配置/凭证/记录。
- 不用旧 `bridge.env` 或旧 R001 测试凭证。
