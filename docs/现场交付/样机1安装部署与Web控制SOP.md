# 样机1 安装部署与 Web 控制 SOP

> 面向现场负责人，非开发者也能逐步执行。
> 总原则：**install ≠ start**，**ACK accepted ≠ physical stationary**，**Web STOP ≠ 物理急停**。
> 现场唯一入口是 `firebotctl`；底层脚本（server-deploy.sh / vehicle-install.sh / vehicle-runtime.sh）与 vim/nano/sed 只允许出现在文末「开发/debug 附录」，正式正文不使用。

---

## 1. 产品化流程总览

除 Tailscale 登录 + 输入 `DEVICE_ID` 外，不手填任何配置、不编辑 env、不执行 SQL。

```text
服务器（管理员，一次）：
  firebotctl fleet register <DEVICE_ID>          # 签发 per-device credential + 一次性 token
  firebotctl server deploy --sha <FINAL_SHA>      # 一条命令部署

车端（现场，每台一次）：
  firebotctl vehicle enroll <DEVICE_ID> --token <TOKEN>
  firebotctl vehicle install --sha <FINAL_SHA>
  firebotctl vehicle verify                        # 必须 VEHICLE_VERIFY=PASS，CONTROL_ENABLED=NO

硬件上线（模块化，一个命令一件事）：
  firebotctl vehicle start base
  firebotctl vehicle start navigation
  firebotctl vehicle verify

STOP 验收（静止态）：
  firebotctl vehicle capability stop-only
  firebotctl vehicle start control
  → Web 静止态 STOP → ACK accepted + 5 distinct fresh zero + STATIONARY_CONFIRMED
  → 服务器 attest 写入 STOP_FIELD_VERIFIED=1

STOP 验收通过后：
  firebotctl vehicle capability patrol            # 无 STOP evidence 会 REJECTED
  → 低速短距离 PATROL_START → 运动中 STOP 再次确认
```

---

## 2. 服务器部署

- 执行：
  ```bash
  firebotctl fleet register <DEVICE_ID>
  firebotctl server deploy --sha <FINAL_SHA>
  firebotctl server verify
  ```
- PASS 条件：`SERVER_DEPLOY=PASS`，`IMAGE_SHA_MATCH=PASS`，`TASK_WORKER_HEARTBEAT=PASS`，required services 全 HEALTHY。
- FAIL 停止：任一镜像 revision ≠ FINAL_SHA 或任一服务 unhealthy，禁止车端安装。
- 禁止：无 `BACKUP_VERIFIED=1` 跑 migrate；重启 postgres/redis/mosquitto/nginx/mediamtx。

## 3. 车端接入（enroll + install）

- 执行：
  ```bash
  firebotctl vehicle enroll <DEVICE_ID> --token <TOKEN>
  firebotctl vehicle install --sha <FINAL_SHA>
  firebotctl vehicle verify
  ```
- PASS 条件：`ENROLL=PASS`（per-device credential 自动写 0600）、`VEHICLE_INSTALL_VERIFY=PASS`（Bridge/Control APPROVED_RUNTIME 均 = FINAL_SHA）、`VEHICLE_VERIFY=PASS`、`CONTROL_ENABLED=NO`。
- FAIL 停止：token 无效/已使用（enroll 会 401 拒绝）、两边 SHA 不一致、catkin_make 失败（自动回滚 previous 后 INSTALL=FAIL）。
- 禁止：手填 MQTT password；enroll 后自动 enable/start 控制；运动。

## 4. 硬件上线

- 执行：`firebotctl vehicle start base` → `firebotctl vehicle start navigation` → `firebotctl vehicle verify`
- PASS 条件：`REAL_PRECHECK=PASS`（硬件 BASE_DEVICE + control_mode==3 + odom + cmd_vel 订阅者 + AMCL）。
- FAIL 停止：任一 FAIL，禁止 capability stop-only 与 start control。
- 禁止：一个命令同时拉起底盘/导航/控制/test battery。

## 5. STOP 能力开放（capability stop-only）

- 执行：`firebotctl vehicle capability stop-only`
- PASS 条件：`CAPABILITY_PROMOTION=PASS level=stop-only` + `NEW_BOOT_ID`（重启成功 + MQTT connected）。
- FAIL 停止：Bridge 未产生新 boot 或 MQTT 未连接，禁止 start control。
- 禁止：`systemctl restart || true`；改 bridge.env。

## 6. Control 启动

- 执行：`firebotctl vehicle start control`
- PASS 条件：`CONTROL_START=PASS`（SHA 一致 + ROS master + control_mode==3 + cmd_vel 订阅者；patrol 额外 AMCL/move_base/pose_navi）。
- FAIL 停止：`CONTROL_START=REJECTED`，禁止 Web STOP。
- 禁止：nohup 直接跑 adapter（正式只走 systemd）。

## 7. Web 静止 STOP 验收

- 执行：车辆静止（IDLE + stopReady）→ Web 点「停止」。
- PASS 条件：`command_ack=accepted` 且 `motion_stop_state=STATIONARY_CONFIRMED`（二者缺一不可）。
- FAIL 停止：只 ACK 未确认静止，或 5 帧不达，禁止 patrol。
- 禁止：把 IDLE 当 STOP 禁用；把 ACK 当物理静止。

## 8. PATROL 能力开放（须 STOP evidence）

- 执行（STOP 验收通过、服务器 attest 写入 `STOP_FIELD_VERIFIED=1` 后）：`firebotctl vehicle capability patrol`
- PASS 条件：`CAPABILITY_PROMOTION=PASS level=patrol`。
- FAIL 停止：无 STOP field evidence → `PATROL_CAPABILITY_PROMOTION=REJECTED`。
- 禁止：先 PATROL 再补 STOP 验收；把当前实现描述成「完整多航点自动巡检」。

## 9. location 开放（声明式）

- 执行：`firebotctl vehicle location enable`
- PASS 条件：`LOCATION_PROMOTION=PASS`（map identity + AMCL + odom 全过才开启；状态写入 assignment，profile-sync 不重置）。
- FAIL 停止：`MAP_IDENTITY_NOT_VERIFIED` 或 `ROS_SOURCE_NOT_READY`。
- 禁止：`sed` 改 bridge.env 或 `FIREBOT_LOCATION_ENABLED=true` 手改。

## 10. 回滚

- 车端：`firebotctl vehicle rollback`（Bridge+Control 回 previous，重编译，`VEHICLE_ROLLBACK=PASS`）。
- 服务器：`firebotctl server rollback`（回 previous SHA，重新 all-app）。
- 禁止：回滚后仍保留错误版本运行。

## 11. 证据收集

- 执行：`firebotctl vehicle support-bundle`（脱敏 tar.gz）+ `firebotctl server verify` 输出 + 部署 `manifest.json`。
- PASS 条件：所有证据与 FINAL_SHA 一致，不含 secret。
- 禁止：把未验证项写 PASS。

---

## 附：开发 / debug 附录（非现场正式流程）

以下命令仅开发/排障用，现场正式正文不使用：

```bash
# 底层脚本（等价实现）
scripts/server-deploy.sh status|preflight|migrate|control-plane|all-app|verify|rollback
integration/ros1/vehicle-install.sh bridge|control|all|verify|rollback
integration/ros1/vehicle-runtime.sh status|ros-base|navigation|control-start|control-stop|real-precheck

# 排障只读（不改配置）
systemctl status firebot-bridge firebot-control
docker compose -f docker-compose.server.yml ps
rostopic list / rostopic info /cmd_vel

# 禁止现场使用：vim/nano/sed -i 改 /etc/firebot/*.env、手改 supported_commands、SQL update、改源码
```
