# R002 跨 Broker 公网迁移（真实车辆 firebot-vehicle-02）

> 本文件是真实车辆 R002 从旧现场 Broker 迁移到 U-Dao 公网的唯一操作依据。
> 身份/网络法证结论见 `docs/会话上下文.md`（本地）与阶段报告（SERVER_R002_IDENTITY_CORRELATION_AUDIT）。
> **迁移完成前本文件所列步骤均为计划；执行需独立阶段授权。**

## 1. 身份与网络事实（法证结论）

```text
人类名称                = R002
稳定协议身份            = firebot-vehicle-02（唯一永久身份；R002 只是显示名称）
VEHICLE_ID_CONFIDENCE   = HIGH

LEGACY_BROKER           = 100.110.31.112:8883（旧现场服务器，经 Tailscale）
NEW_BROKER              = firebot.youdaozd.com:8883（U-Dao 公网）
MIGRATION_TYPE          = CROSS_BROKER（旧 Broker 不是 U-Dao；跨服务器迁移）
TAILSCALE               = ROLLBACK_CHANNEL_ONLY
```

- 服务器关联审计：Robot row / credential / Mosquitto user 均无 `firebot-vehicle-02`；
  云端 `R002` 是 2026-10-27 测试账号，**绝对禁止**复制/改名/复用为真实车辆身份。
- 身份建立走 `firebotctl fleet preregister firebot-vehicle-02 --name R002 --site-code <站点>`：
  `firebot-vehicle-02` 是协议身份，`R002` 是显示名称，不得相互替换。
- 真实车当前 `FIREBOT_VEHICLE_ID=firebot-vehicle-02`、`MQTT_USERNAME=firebot-vehicle-02`、
  `MQTT_HOST=100.110.31.112`（旧 Broker）。
- 若真实车提前直连 `firebot.youdaozd.com:8883`：因 `firebot-vehicle-02` 用户名不存在于
  U-Dao Mosquitto，会被拒绝（fail-closed 正面语义），不存在旧凭据偷偷复用路径。

## 2. Tailscale 铁律

迁移完成并验证前：**不得卸载、关闭、logout Tailscale。**

Tailscale 只保留为回滚通道（旧 Broker 100.110.31.112 的回退路径）。
只有在新链路只读验收全 PASS 且回滚窗口关闭后，才在后续独立阶段处置 Tailscale。

## 3. 迁移前（车端，root-only 备份）

以下快照只能存在车端 root-only 路径（建议 `/root/firebot-legacy-backup-<日期>/`，
`chmod 700`）；**不得**：打印 secret、上传 Git、放 support bundle、发送聊天。

```text
backup 清单：
  /etc/firebot/ 整个目录快照（bootstrap.env / bridge.env / bridge-secret.env /
                  device.env / assignment.env / production-ca.crt / mqtt-ca.crt …）
  旧 /opt/firebot/vehicle-bridge/（含 APPROVED_RUNTIME.txt）
  旧 systemd unit（/etc/systemd/system/firebot-bridge.service / firebot-control.service）
  旧 APPROVED_RUNTIME（/etc/firebot/release.env 或 APPROVED_RUNTIME.txt 内容）
  Tailscale 状态记录（tailscale status；节点名 firebot-vehicle-02）
```

执行迁移前用 `firebotctl vehicle readonly-preflight --phase bootstrap --device-id firebot-vehicle-02`
逐项确认，任何 FAIL 不得开始切换。

## 4. 迁移步骤（正式流程，每步一个命令）

```text
服务器（授权后）：
  firebotctl fleet preregister firebot-vehicle-02 --name R002 --site-code <站点>
  → PREREGISTER=CREATED；PLATFORM_ENABLED=false；SUPPORTED_COMMANDS=[]
  firebotctl fleet register firebot-vehicle-02
  → 现场即时签发 ENROLL_TOKEN（一次性；本命令仅在车端 bootstrap 全部就绪后执行）

车端（现场，按顺序）：
  firebotctl vehicle readonly-preflight --phase bootstrap --device-id firebot-vehicle-02
  firebotctl vehicle enroll firebot-vehicle-02 --token <TOKEN>
  firebotctl vehicle install --sha <FINAL_SHA>
  firebotctl vehicle verify                      # CONTROL_ENABLED=disabled + CONTROL_ACTIVE=inactive
  firebotctl vehicle readonly-preflight --phase enrolled
  firebotctl vehicle readonly-start              # 唯一允许启动 Bridge 的入口
  → READONLY_BRIDGE_START=PASS + CONTROL_ACTIVE=NO

只读观测（服务器）：
  check_real_vehicle_readonly.py → REAL_ROBOT_READONLY_READY=YES 前禁止任何控制步骤
```

## 5. 回滚（公网迁移失败）

```text
1. stop 新 Bridge（systemctl stop firebot-bridge，DEVELOPER/DEBUG；或保留只读停止）
2. 从 /root/firebot-legacy-backup-*/ 恢复：
   - /etc/firebot/*（含旧 production-ca.crt / 旧 bridge-secret）
   - /opt/firebot/vehicle-bridge（旧 APPROVED_RUNTIME）
   - systemd unit + daemon-reload
3. 启动旧 Bridge，验证 mqtt_connected 回连旧 Broker 100.110.31.112:8883
4. Tailscale 原状保留（回滚通道语义）
```

- U-Dao 上新建的 `firebot-vehicle-02` identity **不需要删除**：保持 OFFLINE 即可，
  迁移重试时直接复用（fleet register 的 credential ensure 语义保证不旋转）。
- 任何回滚动作不改变云端 DOWNLINK=false / ALLOWLIST=empty。

## 6. 只读铁律（迁移全程）

```text
CONTROL_ENABLED=NO；CONTROL_ACTIVE=NO；FIREBOT_SUPPORTED_COMMANDS=（空）
DOWNLINK=false；DOWNLINK_ALLOWLIST=empty
Bridge 永不发布 /cmd_vel；ROS Master/Base/Navigation 全程不启动
本迁移不包含任何控制命令；真车控制需独立阶段授权。
```
