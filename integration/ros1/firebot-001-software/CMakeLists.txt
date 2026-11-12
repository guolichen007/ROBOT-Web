cmake_minimum_required(VERSION 3.0.2)
project(firebot_001_software)

find_package(catkin REQUIRED COMPONENTS
  rospy
  std_msgs
  message_generation
)

add_message_files(FILES
  SoftwareCommand.msg
  SoftwareFeedback.msg
  SoftwareStatus.msg
)

generate_messages(DEPENDENCIES std_msgs)

catkin_package(
  CATKIN_DEPENDS rospy std_msgs message_runtime
)

catkin_install_python(PROGRAMS
  src/command_node.py
  src/diagnostics_node.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)
