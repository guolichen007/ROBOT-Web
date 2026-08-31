cmake_minimum_required(VERSION 3.0.2)
project(firebot_control)

find_package(catkin REQUIRED)
catkin_package()

catkin_install_python(PROGRAMS
  scripts/firebot_control_adapter.py
  scripts/test_battery_pub.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)
