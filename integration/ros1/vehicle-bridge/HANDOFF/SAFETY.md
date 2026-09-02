# 安全边界

```text
CONTROL_CODE=PATROL_START,STOP_MOTION
CONTROL_FIELD_VERIFIED=NO
```

Bridge 只做通信层：MQTT 连接、协议校验、上行封装、下行转交、ROS 反馈重封装。

**Bridge 不执行任何真实车辆运动/巡航/急停/灭火/回充/手动控制。**

## 当前安全态

```text
BRIDGE_STUB_MODE=false
FIREBOT_SUPPORTED_COMMANDS=
FIREBOT_SENSORS=
FIREBOT_LOCATION_ENABLED=false
```

## 命令语义

```text
supported_commands=[] 时任何命令在 validator 回 COMMAND_UNSUPPORTED，不转发 ROS。
只有命令已通过 capability 校验并成功转发 ROS 后无 feedback，才回 BRIDGE_ADAPTER_NOT_CONNECTED。
只有 ROS 明确回 ACCEPTED，Bridge 才向 MQTT 回 accepted。
```

## 最终防线

```text
PHYSICAL / VEHICLE SAFETY：
  本地物理急停
  车端安全控制链

LWT：
  只做 offline observability
  不停车
  不制动
  不替代物理急停
```

软件急停不等于物理急停。

## 禁止

```text
- 修改 control flags 为 true
- source_kind migration（先隔离旧 compat 通道）
- 伪造 battery/smoke 数据（真实 provider 未接入时不发布，不造 0）
- 打印/提交 secret、password、token
```
