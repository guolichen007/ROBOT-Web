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

## 4. 迁移步骤（cutover boundary 三段式，每步一个命令）

### A. 旧链路仍在线（cutover 之前）

```text
1. root-only legacy backup（见第 3 节；chmod 700）
2. 车端：
   firebotctl vehicle readonly-preflight --phase bootstrap \
     --device-id firebot-vehicle-02 --expected-sha <FINAL_SHA>
   → READONLY_BOOTSTRAP_PREFLIGHT=PASS（bootstrap 不强制 HEAD==SHA：允许仍处于旧 release）
3. 服务器（授权后）：
   firebotctl fleet register firebot-vehicle-02
   → 现场即时签发一次性 ENROLL_TOKEN
```

**到这里旧 Bridge 仍允许在线。** 收到 token 之前任何失败不进入 rollback。

### B. 正式进入 cutover（唯一切换点）

```text
车端（收到 token 后）：
  firebotctl vehicle migration-stop-legacy
  → LEGACY_BRIDGE_STOPPED=YES + CONTROL_ACTIVE=NO + MIGRATION_CUTOVER_BOUNDARY=ENTERED
  → OLD_BROKER_CONNECTION_EXPECTED=OFFLINE；TAILSCALE=KEEP
```

- Control active/enabled 时该命令 `MIGRATION_STOP_LEGACY=REJECTED / CONTROL_NOT_SAFE`（不自动处理 Control）。
- **从此开始任何失败一律进入 ROLLBACK_REQUIRED=YES（第 5 节），不得“继续试试看”。**

### C. 新链路安装（Bridge 全程 inactive，只有 readonly-start 允许 active）

```text
  firebotctl vehicle enroll firebot-vehicle-02 --token <TOKEN>
     # fail-closed：Bridge active/activating/reloading → ENROLL=REJECTED / LEGACY_BRIDGE_STILL_ACTIVE
     # （不写任何文件、不消费 token——token 必须 Bridge 已停止后才允许消费）
  firebotctl vehicle install --sha <FINAL_SHA>
     # fail-closed：Bridge active → VEHICLE_INSTALL=REJECTED / BRIDGE_ACTIVE_BEFORE_INSTALL
     # （不进入 release_sync / catkin_make；install ≠ start 真正成立）
  firebotctl vehicle verify                        # CONTROL_ENABLED=disabled + CONTROL_ACTIVE=inactive
  firebotctl vehicle readonly-preflight --phase enrolled --expected-sha <FINAL_SHA>
     # EXACT_SHA_MATCH=YES 强制（HEAD == FINAL_SHA）；SUPPORTED_COMMANDS 必须为空
  firebotctl vehicle readonly-start                # 唯一允许新 Bridge active 的入口
     # 启动后验收失败（BRIDGE_ACTIVE/MQTT_CONNECTED/boot_id）→ 自动 disable --now firebot-bridge
     # → FAIL_CLOSED_BRIDGE_STOP=PASS + READONLY_BRIDGE_START=FAIL（不留下半在线 Bridge）

只读观测（服务器）：
  check_real_vehicle_readonly.py → REAL_ROBOT_READONLY_READY=YES 前禁止任何控制步骤
```

## 5. 回滚（cutover 之后任一步失败 → ROLLBACK_REQUIRED=YES）

一旦 `MIGRATION_CUTOVER_BOUNDARY=ENTERED`，之后 enroll / install / verify / enrolled preflight /
readonly-start 任一步失败，**不得继续试错**，立即执行：

```text
1. 确保新 Bridge 已停（readonly-start 失败已自动 fail-closed stop；其余情况手动确认 inactive）
2. 从 /root/firebot-legacy-backup-*/ 恢复（车端 root-only）：
   - /etc/firebot/（含旧 production-ca.crt / 旧 bridge-secret / 旧 bootstrap）
   - /opt/firebot/vehicle-bridge（旧 APPROVED_RUNTIME）
   - /etc/systemd/system/firebot-bridge.service + systemctl daemon-reload
3. systemctl enable/start 旧 firebot-bridge
4. 验证：MQTT_HOST=100.110.31.112 + MQTT_CONNECTED=YES
   → LEGACY_ROLLBACK=PASS（否则不得宣称回滚成功）
5. Tailscale 原状保留（回滚通道语义；迁移期间不得 tailscale down/logout/卸载/禁用）
```

- 云端 `firebot-vehicle-02` Robot row / credential / 已消费的 enrollment token **不删除**：
  保持 OFFLINE、enabled=false、supported_commands=[]、DOWNLINK=false。
- 重试：重新 `firebotctl fleet register firebot-vehicle-02`（credential ensure 语义不旋转，重新签发 token）。

## 6. 只读铁律（迁移全程）

```text
CONTROL_ENABLED=NO；CONTROL_ACTIVE=NO；FIREBOT_SUPPORTED_COMMANDS=（空）
DOWNLINK=false；DOWNLINK_ALLOWLIST=empty
Bridge 永不发布 /cmd_vel；ROS Master/Base/Navigation 全程不启动
本迁移不包含任何控制命令；真车控制需独立阶段授权。
```
