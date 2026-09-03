# 样机1 安装部署与 Web 控制 SOP

> 面向现场负责人，非开发者也能逐步执行。每一步包含：执行命令 / 预期结果 / PASS 条件 / FAIL 停止位置 / 禁止事项。
> 总原则：**install ≠ start**，**ACK accepted ≠ physical stationary**，**Web STOP ≠ 物理急停**。
> 未在本 SOP 中的步骤一律不做；任何一步 FAIL 立即停止，回传主机，不现场改代码。

---

## A. 准备

- 执行：确认样机1（`firebot-vehicle-01`）物理急停可达、无人位于运动区、网络可达。
- 预期：现场具备「低速、短距离、不载人」的第一次运动条件。
- PASS 条件：所有准备项确认无误。
- FAIL 停止：任一安全条件不满足，禁止进入 B。
- 禁止：先运动后补安全确认；跳过物理急停检查。

## B. Git exact SHA

- 执行：
  ```bash
  cd <ROBOT-Web 仓库根>
  git fetch origin
  git checkout integration/server-web-real-vehicle-ready-v1
  git pull --ff-only origin integration/server-web-real-vehicle-ready-v1
  git rev-parse HEAD   # 记录为 FINAL_SHA
  ```
- PASS 条件：`FINAL_SHA` 为 40 位 hex，且与主机回传的批准 SHA 一致。
- FAIL 停止：SHA 不一致或 `git pull` 非 ff。
- 禁止：`git reset` / `git push` / 现场改代码。

## C. Server deployment

- 执行：
  ```bash
  cd <ROBOT-Web 仓库根>
  TARGET_SHA=<FINAL_SHA> ./scripts/server-deploy.sh preflight
  TARGET_SHA=<FINAL_SHA> ./scripts/server-deploy.sh control-plane
  TARGET_SHA=<FINAL_SHA> ./scripts/server-deploy.sh verify
  ```
- 预期：`PREFLIGHT=PASS`、`DEPLOY=PASS scope=control-plane`、`SERVER_SHA=<FINAL_SHA>`、`IMAGE_SHA_MATCH=PASS`。
- PASS 条件：control-plane 四服务（api/mqtt-ingress/command-dispatcher/task-worker）镜像 revision 全部等于 `FINAL_SHA`。
- FAIL 停止：任一 image revision 不一致，禁止车端安装。
- 禁止：重启 postgres/redis/mosquitto/nginx/mediamtx（除非配置变化）；无 `BACKUP_VERIFIED=1` 跑 migrate。

## D. Vehicle installation

- 执行：
  ```bash
  cd <ROBOT-Web 仓库根>/integration/ros1
  FIREBOT_REQUIRE_SHA=<FINAL_SHA> ./vehicle-install.sh all
  FIREBOT_REQUIRE_SHA=<FINAL_SHA> ./vehicle-install.sh verify
  ```
- 预期：`VEHICLE_INSTALL_VERIFY=PASS`，Bridge 与 Control 的 `APPROVED_RUNTIME.txt` 都等于 `FINAL_SHA`。
- PASS 条件：两边 SHA 一致；`/home/tl/firerobot_ws/src/firebot_control/package.xml` 存在。
- FAIL 停止：任一 install 失败即回滚（见 U），不假装成功。
- 禁止：install 后自动 enable/start 控制服务、改 supported_commands、运动。

## E. Bridge configuration

- 执行：编辑 `/etc/firebot/bridge.env`，确认 `SITE_CODE/MAP_CODE/MAP_VERSION/MAP_CHECKSUM`、`FIREBOT_LOCATION_ENABLED=false`、`FIREBOT_SUPPORTED_COMMANDS=`（空）。
- PASS 条件：`BRIDGE_STUB_MODE=false`、`supported_commands` 为空、地图身份完整。
- FAIL 停止：secret（`/etc/firebot/bridge-secret.env`）缺失或配置不符。
- 禁止：写密码进 env；改 secret。

## F. ROS base startup

- 执行：
  ```bash
  cd integration/ros1
  ./vehicle-runtime.sh ros-base
  ./vehicle-runtime.sh status
  ```
- 预期：只启动 roscore + 真实底盘 bringup；`底盘驱动(/robot_status)：运行中`。
- PASS 条件：`/robot_status` 有 publisher，且未启动 navigation/control/test_battery。
- FAIL 停止：底盘驱动未起。
- 禁止：一个命令同时拉起导航/控制/测试 battery。

## G. Hardware precheck

- 执行：`./vehicle-runtime.sh real-precheck`
- PASS 条件：`REAL_PRECHECK=PASS`（硬件在位 + control_mode==3 + /odom + /cmd_vel 订阅者 + AMCL）。
- FAIL 停止：任一项 FAIL，禁止 Q/R。
- 禁止：跳过 precheck 直接运动。

## H. control_mode

- 执行：`timeout 3 rostopic echo -n1 /robot_status | grep control_mode`
- PASS 条件：输出 `control_mode: 3`（ROS 控制模式）。
- FAIL 停止：非 3，禁止一切运动。
- 禁止：把「未知」当 ROS 模式。

## I. odom

- 执行：`timeout 3 rostopic echo -n1 /odom`
- PASS 条件：持续有 `twist.twist.linear/angular` 输出。
- FAIL 停止：无 odom，禁止运动与静止确认。
- 禁止：用手写数值替代真实 odom。

## J. cmd_vel topology

- 执行：`rostopic info /cmd_vel`
- PASS 条件：Subscribers 含真实底盘驱动（igk_robot/serial_485/485/agv），且无第二个非零速度 publisher。
- FAIL 停止：无订阅者或多 publisher，禁止 STOP/运动。
- 禁止：未验证仲裁就 `cmd_vel_arbitration_verified=true`。

## K. navigation

- 执行：`./vehicle-runtime.sh navigation`
- PASS 条件：`/move_base/status` 有 publisher，`/waterplus/navi_pose` 有 subscriber。
- FAIL 停止：move_base 或 pose_navi_server 未起。
- 禁止：手工拼节点；跳过 rosnode cleanup。

## L. AMCL

- 执行：`timeout 3 rostopic echo -n1 /amcl_pose`
- PASS 条件：`/amcl_pose` 有 publisher 且有真实数据。
- FAIL 停止：定位失效，禁止运动。
- 禁止：伪造定位。

## M. map identity

- 执行：核对 `bridge.env` 的 `SITE/MAP_CODE/MAP_VERSION/MAP_CHECKSUM` 与服务器地图一致。
- PASS 条件：与服务器 map 合同一致。
- FAIL 停止：不一致，禁止 location 上行与 patrol。
- 禁止：在未确认地图前打开 `FIREBOT_LOCATION_ENABLED`。

## N. location

- 执行：确认 `FIREBOT_LOCATION_ENABLED=false`（本轮仍保持），location 上行在 map 确认后单独开放。
- PASS 条件：location 未开启（fail-closed）。
- FAIL 停止：若已误开启，关闭并复核 map。
- 禁止：未确认地图就发 canonical location。

## O. capability

- 执行：查看服务器 `verify` 输出的 `capabilities` / `data_channels`。
- PASS 条件：capability 含 `stop_motion`（本轮先只开放 stop_motion）。
- FAIL 停止：能力声明缺失。
- 禁止：现场手工改 `FIREBOT_SUPPORTED_COMMANDS` 为 patrol。

## P. server control readiness

- 执行：`TARGET_SHA=<FINAL_SHA> ./scripts/server-deploy.sh verify`，看 `stop_ready`。
- PASS 条件：`stop_ready=true`（online + control/ack + bidirectional_bridge + command_path + cmd_vel_arbitration + control_mode=3 + capability）。
- FAIL 停止：`stop_ready=false`，按 `readiness_reasons` 逐项补齐，禁止发 STOP。
- 禁止：把 `CANONICAL_MQTT` 当绕过 ROS 运动门的理由；手工把所有 flag 改 true。

## Q. Control Adapter start

- 执行：`./vehicle-runtime.sh control-start`
- PASS 条件：`firebot_control_adapter` 运行中（`vehicle-runtime.sh status`）。
- FAIL 停止：adapter 未起。
- 禁止：`systemctl enable firebot-control`；跳过下游 gate 就 start。

## R. Web STOP（车辆静止状态）

- 执行：Web 监控页，车辆 `IDLE` 且 `停止` 按钮可用时，点击「停止」。
- 预期：`command_ack=accepted` + 5 条独立 fresh zero 遥测 + `STATIONARY_CONFIRMED`。
- PASS 条件：`ACK accepted` 且 `motion_stop_state=STATIONARY_CONFIRMED`（二者缺一不可）。
- FAIL 停止：只 ACK 未确认静止，或 5 帧不达，禁止进入 S。
- 禁止：把 IDLE 当 STOP 禁用；把 ACK 当物理静止。

## S. Web PATROL_START

- 执行：R 通过后，低速短距离发送「开始巡检」。
- 预期：`PATROL_START` 只启动到**首个有效巡检目标**（非完整多航点执行）。
- PASS 条件：车辆按现有 ROS 导航向首个目标低速移动。
- FAIL 停止：任何异常立即 STOP。
- 禁止：把当前实现描述成「完整自动巡检」；先 PATROL 再补 STOP 验收。

## T. moving STOP（运动中 Web STOP）

- 执行：车辆运动中点击「停止」。
- 预期：cancel_all_goals + 零速 burst + `ACK accepted` + `STATIONARY_CONFIRMED`。
- PASS 条件：同上 R。
- FAIL 停止：未确认物理静止，禁止后续任何运动。
- 禁止：依赖 Web STOP 应对网络中断。

## U. rollback

- 执行（车端）：install.sh 保留 `previous`，一步回滚：
  ```bash
  sudo mv /opt/firebot/vehicle-bridge /opt/firebot/_bad \
    && sudo mv /opt/firebot/.vehicle-bridge.previous /opt/firebot/vehicle-bridge \
    && sudo systemctl restart firebot-bridge
  mv /home/tl/firerobot_ws/src/firebot_control /tmp/firebot_control.bad \
    && mv /home/tl/firerobot_ws/.firebot_control.previous /home/tl/firerobot_ws/src/firebot_control \
    && cd /home/tl/firerobot_ws && catkin_make
  ```
- 执行（服务器）：`TARGET_SHA=<FINAL_SHA> ./scripts/server-deploy.sh rollback`。
- PASS 条件：回滚后 `verify` 重新 PASS。
- 禁止：回滚后仍保留错误版本运行。

## V. evidence collection

- 执行：收集并归档：`FINAL_SHA`、`server-deploy` 的 `manifest.json`、`APPROVED_RUNTIME.txt` 两份、`verify` 输出、STOP 的 `command_ack` + `RobotOperationEvent`（STATIONARY_CONFIRMED）。
- PASS 条件：所有证据与 `FINAL_SHA` 一致，不含 secret。
- 禁止：把未验证项写 PASS；把历史快照当当前状态。
