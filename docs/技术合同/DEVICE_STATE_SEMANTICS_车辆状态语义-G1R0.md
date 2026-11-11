# 车辆状态语义合同（G1-R0）

> 只读 Agent（`firebot-vehicle-01`）上报的设备状态语义，与云端字段的对应关系。
> 本文件是车端↔云端对状态含义的**唯一解释**：任何一方不得按自己的猜测赋予新含义。

## 1. 五个基础状态

| 状态 | 云端表现（现有 Schema，不改动） | 车端证据 | 语义边界 |
| --- | --- | --- | --- |
| `AGENT_ONLINE` | `online_state=ONLINE`（availability online 或新鲜 status 驱动） | Agent 进程存活、MQTT 连接建立、status 每 3s 新鲜上报 | **只证明 Agent 与链路在线**，不证明任何机器人程序可用 |
| `CHASSIS_NOT_CONNECTED` | `programs.chassis=false`（`agent_state_json.status.programs`） | 工控机上未观测到底盘驱动进程 | **“未连接/未观测到”**，不是“故障”，不是“就绪”的否定承诺 |
| `NAVIGATION_NOT_READY` | `programs.navigation=false` | 未观测到导航程序进程 | 同上：**只观测、不推断** |
| `SENSOR_UNKNOWN` | `programs.{smog,thermal,battery,camera}=false`；telemetry 对应字段显式 `null` | 未接入相应传感器，无真实数据源 | **未知 ≠ 0 ≠ 正常**：平台不得把 null/false 当作测量值展示 |
| `CONTROL_DISABLED` | 云端 `VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false`、allowlist 为空；该车无任何 Command 记录 | Agent 结构上无订阅路径（transport.subscribe 即抛错）、无任何控制输出 | 本阶段（G1 及之前）控制面**整体关闭**，与车端状态无关 |

## 2. 只读 Agent 的推断禁令

1. **不靠 `task=idle` 推断底盘就绪**：`task.state=idle` 只表示“Agent 不执行任务”，
   是只读 Agent 的固定语义，与底盘/导航状态无因果关系。
2. **不靠进程名匹配推断就绪**：`programs.*` 来自 /proc 进程观测
   （`collector.process_liveness`），仅回答“该名字的进程是否在运行”。
   进程存在 ≠ 硬件就绪；进程不存在 ≠ 硬件故障。就绪判定属于未来接入底盘后的
   控制面工作（G1 之后），本阶段不做。
3. **绝不生成模拟数值**：未接入的底盘/导航/电池/定位/烟雾/热成像在 telemetry
   中显式 `null`、在 programs 中为 `false`，不存在“0”“100%”等伪装值。

## 3. host 指标可见性（未修改云端 Schema）

- Agent 采集的真实 OS 指标（OS 版本/内核/uptime/负载/内存/磁盘/Agent 运行状态）
  **默认只写车端本地日志**（`host-metrics.jsonl`），上行默认关闭。
- 因此当前**云端/Web 不展示 host 指标**；本仓库不宣称已实现该展示。
- 上线路径见《VEHICLE_AGENT_V1_HOST_METRICS_EXTENSION.md》（先入正式 Schema、
  再灰度开启，未验证字段不进入生产）。

## 4. 反证测试

- `test_status_payload_validates_against_contract` / `test_status_semantics_*`：
  status 载荷结构合法，且 `programs` 中机器人程序键在无进程观测时恒为 false。
- `test_no_fake_hardware_and_control_gate`（真实链路 E2E）：API 侧
  `battery=None`、`estop_active=False`、`current_mode=IDLE`、无 Command 记录、
  downlink=false、allowlist 为空、Mosquitto 日志无 Agent SUBSCRIBE。
- `test_unknown_extra_fields_ignored_by_platform`：未知字段（含实验性 host）
  被平台 `extra=ignore` 丢弃，不进入业务状态。
