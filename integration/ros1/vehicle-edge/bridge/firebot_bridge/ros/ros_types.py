"""真实 ROS message 类（禁止字符串类名）。

rospy.Publisher/Subscriber 必须使用真实 import 的 message class，而非
"std_msgs/String" 之类的字符串。rospy 不可用时（非 ROS 环境/单测）所有类为 None。
"""
from __future__ import annotations

try:
    from std_msgs.msg import Float32 as StdFloat32
    from std_msgs.msg import String as StdString
    from nav_msgs.msg import Odometry
    from geometry_msgs.msg import PoseWithCovarianceStamped

    _ROS_TYPES_OK = True
except Exception:  # noqa: BLE001 — rospy 未安装
    StdFloat32 = None
    StdString = None
    Odometry = None
    PoseWithCovarianceStamped = None
    _ROS_TYPES_OK = False
