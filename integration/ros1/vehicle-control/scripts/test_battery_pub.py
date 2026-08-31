#!/usr/bin/env python3
"""TEST battery publisher（明确标记 TEST_INJECTED，非真实电量）。

仅用于 R1 阶段验证「ROS battery → /firebot_bridge/battery → Bridge → MQTT」上行链路。
生产环境必须替换为真实电量源，禁止使用本脚本作为生产 fallback。
"""
import rospy
from std_msgs.msg import Float32

rospy.init_node("test_battery_pub")
pub = rospy.Publisher("/firebot_bridge/battery", Float32, queue_size=10)
rate = rospy.Rate(1.0)
VALUE = rospy.get_param("~value", 82.4)

rospy.logwarn(
    "BATTERY_SOURCE=TEST_INJECTED  REAL_BATTERY_VERIFIED=NO  value=%.1f", VALUE
)
while not rospy.is_shutdown():
    pub.publish(Float32(data=VALUE))
    rate.sleep()
