# 下一阶段（需用户另行批准后才能执行）

## 已完成（2026-08-31）

```text
patrol 下行信号链：服务器 → MQTT → Bridge → ROS adapter → fail-closed feedback → ACK = PASS
（真实导航当时未就绪，REJECTED / NAV_EXECUTION_NOT_READY，属正确 fail-closed）
```

## 下一阶段 = 真实数据 provider + 真实导航运动验证

只做 read-only 数据接入与受控验证，一次一个 provider。

目标：

```text
真实 battery  → /firebot_bridge/battery
真实 status   → /firebot_bridge/status
真实 location/odom → /firebot_bridge/location
真实 smoke    → /firebot_bridge/smoke（若有真实源）
```

原则：

```text
真实数据优先。
不造 0。
不造假 battery。
一次只接一个 provider。
真实运动（navigation execution）单独受控验证，默认 fail-closed。
```

禁止：

```text
rostopic pub / rosservice call / roslaunch 手动控制
伪造 provider 数据
一次验证中同时切换多个变量
source_kind 迁移（先隔离旧 compat 通道）
```

数据路线：

```text
real ROS source
→ canonical Bridge provider
→ Vehicle Bridge
→ MQTT
→ Server
→ Web
```

控制（下行 real motion）最后处理，且在真实硬件 + fail-closed gate 下单独验证。
