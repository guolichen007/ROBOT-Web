# R002 跨 Broker 迁移（Vehicle Edge 版流程）

> 权威文档：`docs/部署运维/R002跨Broker公网迁移.md`（cutover boundary A/B/C 不变）。
> 本文件是 Vehicle Edge 工程化后的命令映射。

```text
A. PRE-CUTOVER / OLD LINK ONLINE（旧链路仍在线，零停机）
   1. firebotctl vehicle edge-init --runtime-user tl \
        [--ros-setup /opt/ros/noetic/setup.bash]
        [--ros-workspace-setup /home/tl/firerobot_ws/devel/setup.bash]
   2. firebotctl vehicle edge-backup-legacy
      → LEGACY_BACKUP=PASS BACKUP_ROOT=/home/tl/firebot-edge/backups/legacy/<UTC>/
      （canonical 单根备份；root:root 0700；零运行影响）
   3. stage 两个文件（服务器下发）：
      /home/tl/firebot-edge/config/bootstrap.env  → FIREBOT_SERVER_URL=https://firebot.youdaozd.com
      /home/tl/firebot-edge/certs/mqtt-ca.crt     → U-Dao private CA（公开信任材料）
   4. firebotctl vehicle readonly-preflight --phase bootstrap \
        --device-id firebot-vehicle-02 --expected-sha <FINAL_SHA>
      → legacy bridge ACTIVE 允许（COMMUNICATION_OWNER=LEGACY /
        PRE_CUTOVER_LEGACY_LINK_PRESERVED=YES）；新 Edge inactive 必须；
        Edge active → EDGE_STARTED_BEFORE_CUTOVER FAIL
   5. 服务器：firebotctl fleet register firebot-vehicle-02（现场即时签发 token）

B. 正式进入 cutover（唯一切换点）
   firebotctl vehicle migration-stop-legacy
   → LEGACY_BRIDGE_STOPPED=YES + MIGRATION_CUTOVER_BOUNDARY=ENTERED
   → 此后任何失败一律 ROLLBACK_REQUIRED=YES（见回滚.md）

C. 新链路安装（Bridge/Edge 全程 inactive，只有 readonly-start 允许 active）
   firebotctl vehicle enroll firebot-vehicle-02 --token <TOKEN>
   firebotctl vehicle edge-install --sha <FINAL_SHA>
   firebotctl vehicle edge-verify --sha <FINAL_SHA>
   firebotctl vehicle readonly-preflight --phase enrolled --expected-sha <FINAL_SHA>
   firebotctl vehicle readonly-start          # 启动 firebot-vehicle-edge（新 service）
   vehicle-connection-status                  # 期望 Overall HEALTHY

D. 新链路失败 → 回滚（见回滚.md；唯一入口 = edge-rollback）
   firebotctl vehicle edge-rollback          # 恢复 legacy 通信 owner
   → LEGACY_ROLLBACK=PASS / MQTT_HOST=100.110.31.112 / MQTT_CONNECTED=YES

只读观测：平台 check_real_vehicle_readonly.py；
REAL_ROBOT_READONLY_READY=YES 前禁止任何控制步骤。
```

- 旧 firebot-bridge.service 保留 disabled/inactive 作为回滚 artifact；
  migration-stop-legacy 停的是 firebot-bridge.service。
- 迁移期间 TAILSCALE=ROLLBACK_CHANNEL_ONLY。
- 生产 tools release（管理平面）：交付时 `install-tools-release.sh` 一次性 bootstrap 到
  `/home/tl/firebot-edge/tools/`，`/usr/local/bin/firebotctl -> tools/current/firebotctl`；
  生产 mode（VEHICLE_TOOLS_RELEASE）无 Git 依赖、无 server/fleet scope。
- 通信迁移回滚唯一入口 = `firebotctl vehicle edge-rollback`（≠ `vehicle rollback`）。
