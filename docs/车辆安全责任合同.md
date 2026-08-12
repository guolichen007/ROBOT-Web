# 车辆安全责任合同

云平台不能承担网络失联后的最终运动安全闭环。真实车端必须：manual 500ms TTL 到期本地停止；断网不无限运动；过期/错误 target_boot 命令不执行；command_id 幂等；重启后旧 boot 不重放；software e-stop accepted 后锁存；reset 显式；硬件急停优先。

ACK accepted 表示车端应用层本地校验通过并接受执行，不只是 MQTT 收包。平台显示“stop/e-stop 已发送”不等于车辆已经停止；未收到 ACK 只能显示未确认。

Mock 模拟以上合同不代表真实 ROS2、底盘、执行机构或现场安全认证已经完成。
