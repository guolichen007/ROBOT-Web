# firebot-001 SOFTWARE_TEST 软件闭环（无硬件执行器）

设备专属测试命名空间：`/firebot_001/software/{command,feedback,status,diagnostics}`。
本包只运行软件状态机与诊断采集；不连接 GPIO/CAN/Modbus/继电器/电机/底盘；
所有反馈 `software_test=true` 且日志带 `[SOFTWARE_TEST]` 标记。
**禁止**将 R002 真实设备驱动（firerobot_ws 中 ugv_sdk/battery_sdk 等）复制到本工作空间运行。

## 构建与运行（隔离工作空间）

```bash
mkdir -p ~/firebot_001_ws/src
ln -s <仓库>/integration/ros1/firebot-001-software ~/firebot_001_ws/src/
cd ~/firebot_001_ws && source /opt/ros/noetic/setup.bash && catkin_make
source devel/setup.bash
export ROS_MASTER_URI=http://localhost:11311   # Master 仅本机访问
roscore &                                     # 或 roslaunch firebot_001_software software_test.launch
roslaunch firebot_001_software software_test.launch
```

## 软件命令示例（真实日志与反馈）

```bash
rostopic pub -1 /firebot_001/software/command firebot_001_software/SoftwareCommand \
  "{command_id: 'G2-0001', action: 'start', params: '{}'}"
rostopic echo /firebot_001/software/feedback
```
