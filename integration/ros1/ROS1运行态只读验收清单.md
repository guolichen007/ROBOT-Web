# ROS1 运行态只读验收清单

## 绝对禁止

- [ ] 不执行 `rostopic pub`。
- [ ] 不执行 `rosservice call`。
- [ ] 不发送 action goal。
- [ ] 不修改 launch/参数/ROS 节点。
- [ ] 不启用任何 Firebot 下行或移动车辆。

## 只读采集

```bash
set -e
source /opt/ros/noetic/setup.bash
source ~/firerobot_ws/devel/setup.bash
rosnode list
rostopic list -t
rostopic info /amcl_pose
rostopic echo -n 3 /amcl_pose
rostopic hz /amcl_pose
rostopic info /odom
rostopic echo -n 3 /odom
rostopic hz /odom
rostopic info /robot_status
rostopic echo -n 3 /robot_status
rostopic hz /robot_status
rostopic echo -n 1 /move_base/status
rosrun tf tf_echo map base_link
```

## 必须记录

- [ ] `/amcl_pose` 实际频率、frame、位置、四元数与协方差样例。
- [ ] `/odom` 实际 `vx/vy/wz` 与频率，验证麦克纳姆横移数据存在。
- [ ] `/robot_status` 完整字段、电池单位与实际 `control_mode`。
- [ ] 实际启动 launch，确认是 mode 0 还是 mode 3。
- [ ] 物理车头与 URDF `+X` 是否一致。
- [ ] `mymap.yaml + mymap.pgm` 的版本与 SHA-256。
- [ ] 主控管理 IP 与 MQTT 出口网络。
- [ ] `/dev/ttyUSB1` 对应 BMS 或烟雾设备，避免两个脚本抢占。

## 平台只读验收

- [ ] 外部设备 ID 经人工绑定到 R001。
- [ ] pose/odom/status/battery/heartbeat 连续 30 分钟无结构错误。
- [ ] `vy != 0` 时 Web/Redis 的 `planar_speed` 正确，静止帧不增加。
- [ ] `/odom` 位姿不会覆盖 AMCL 全局位置。
- [ ] control mode 3 不显示成“巡检中”。
- [ ] 软件急停、reset、灭火、烟雾、红外、视频显示“不支持/未接入”。
- [ ] 所有控制按钮不可用，API 返回 `ROS_COMPAT_READ_ONLY`。
- [ ] nav result 只产生诊断记录，不完成任何 Firebot task。
- [ ] 断流达到阈值后显示 STALE/OFFLINE，恢复不重放命令。

## 结论字段

```text
ROS1_RUNTIME_INTERFACE_VERIFICATION=PASS/FAIL
READY_FOR_ROS1_READONLY_INTEGRATION=YES/NO
READY_FOR_STOP_MOTION_TEST=NO
READY_FOR_MANUAL_MOTION_TEST=NO
READY_FOR_SOFTWARE_ESTOP_TEST=NO
READY_FOR_PATROL_TEST=NO
```
