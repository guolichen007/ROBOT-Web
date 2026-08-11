# VEHICLE 安全责任合同

云平台不承担网络失联后的最终运动安全闭环。车端必须实现 manual 500ms TTL watchdog、断网停止、过期命令拒绝、command_id 幂等、旧 boot 防重放、software e-stop 锁存与显式 reset；硬件急停始终优先。平台显示“已发送”不等于车辆已经停止。
