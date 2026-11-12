# firebot-001 G2 车端接口契约（供开发 CC 对接）

> 状态：G1 真实联通运行中；本文件定义 G2 下行→ROS 软件执行的对接面。
> 车端当前只有 SOFTWARE_TEST 软件闭环，**无硬件执行器**（GPIO/CAN/Modbus/继电器/电机/底盘全部 NONE）。

## 1. ROS 命名空间与话题（设备专属）

| 话题 | 类型 | 方向 | 说明 |
| --- | --- | --- | --- |
| `/firebot_001/software/command` | `firebot_001_software/SoftwareCommand` | 车端订阅 | 云端命令（G2 适配器转发至此） |
| `/firebot_001/software/feedback` | `firebot_001_software/SoftwareFeedback` | 车端发布 | 命令执行反馈（含拒绝原因） |
| `/firebot_001/software/status` | `firebot_001_software/SoftwareStatus` | 车端发布（latch） | 软件状态机 + 进程诊断快照 |
| `/firebot_001/software/diagnostics` | `std_msgs/String` | 车端发布（0.2Hz） | 进程存活/负载/内存 JSON 文本 |

ROS Master：`http://localhost:11311`（本机/受控测试网络）。

## 2. 消息格式（.msg 定义见 `msg/` 目录）

```text
SoftwareCommand:
  string command_id   # 云端 command_id/msg_id 原样透传（UUID 字符串，无损）
  string action       # start | close | patrol | stop_patrol | status
  string params       # JSON 字符串（patrol 支持 {"rounds": N}，默认 3）

SoftwareFeedback:
  string command_id   # 与 SoftwareCommand.command_id 逐字节一致
  string action
  string status       # ACCEPTED | RUNNING | COMPLETED | REJECTED | STOPPED
  string detail       # 人类可读说明（含拒绝原因）
  bool   software_test  # 恒为 true（SOFTWARE_TEST 标记）
  string ts           # ISO-8601 UTC

SoftwareStatus:
  string state        # idle | ready | patrol | fault
  float64 uptime_seconds
  int32  pid
  float64 load_1m
  float64 mem_available_bytes
  string diagnostics  # 含 hardware_executors=NONE 声明
  string ts
```

## 3. 状态机语义（真实执行，非假装成功）

| 命令 | 前置 | 成功路径（feedback 序列） | 拒绝路径 |
| --- | --- | --- | --- |
| start | 任意→ready | ACCEPTED → COMPLETED(state=ready) | 已 ready → REJECTED(already ready) |
| close | ready/patrol→idle | ACCEPTED → COMPLETED(state=idle) | — |
| patrol | state=ready | ACCEPTED → RUNNING(每轮1条, 默认3轮) → COMPLETED | 未 ready → REJECTED(not ready) |
| stop_patrol | state=patrol | ACCEPTED → STOPPED(于下一轮边界) | 未 patrol → REJECTED(not patrolling) |
| status | 任意 | COMPLETED(state=<当前>) | — |
| 未知 action / 非法 params JSON | — | — | REJECTED(原因见 detail) |

节点真实日志前缀 `[SOFTWARE_TEST]`，含 command_id；所有状态迁移真实发生并可观测
（状态话题 latch + 日志 + feedback 三处一致）。

## 4. command_id 无损对应契约

1. `SoftwareCommand.command_id` 与 `SoftwareFeedback.command_id` 均为 string，按字节
   原样透传云端 `command_id`（UUID 字符串 ≤64 字符）；车端不截断、不转换大小写、
   不重写。
2. 同一 command_id 的所有 feedback 必须能在云端与「Web 命令记录 → 云端日志 → MQTT
   → 车端日志 → ROS feedback」五处逐条对应。
3. G2 适配器负责：重复 command_id 幂等拒绝、过期命令（ts 超出窗口）拒绝、会话不匹配
   拒绝——车端状态机只执行适配器转发的合法命令（本契约不含防护实现，防护在 G2 冻结
   版本内验收）。

## 5. 源码与运行进程（真实）

| 项 | 值 |
| --- | --- |
| 源码（仓库） | `integration/ros1/firebot-001-software/`（msg/、src/command_node.py、src/diagnostics_node.py、launch/software_test.launch） |
| 隔离工作空间 | `/home/tl/firebot_001_ws`（软链至仓库包，devel 构建） |
| 运行进程 | rosmaster(pid 3388187)、command_node.py(pid 3437254)、diagnostics_node.py(pid 3437255)；持续运行，状态 idle |
| 实测记录 | start/close/patrol/stop_patrol 全链路 feedback 与日志证据（G2-SW-0001…0009） |

## 6. G2 前置条件（车端侧已满足项）

- G1 Agent 2580c96e 正式运行（TLS/PUBACK/上行零错误）；unit disabled 手动管理。
- 身份/CA 就位（R001-Prod-CA）；旧桥 inactive 未动。
- ROS Noetic + 软件闭环运行中；不重装 ROS、不启动真实驱动。
- 车端等待开发 CC 的 **G2 冻结版本**（双向执行器 + 软件适配器）交付后执行安装与
  单 owner 切换方案；切换前按批准流程备份当前 G1 Agent 与 unit。
