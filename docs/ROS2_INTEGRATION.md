# ROS2 现场集成入口

最终冻结合同与可交付文件位于 [integration/ros2](../integration/ros2/README_现场对接说明.md)。现场按 Broker/TLS → heartbeat/capabilities → location/map → status/sensor/time → command/ACK → stop → low-speed manual/TTL → e-stop → patrol → fire → extinguish 的 Gate 顺序执行。

本仓库不包含 ROS2 node、SLAM、Nav2、驱动、底盘或执行机构实现。
