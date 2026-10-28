# R002 跨 Broker 迁移（Vehicle Edge 版流程）

> 权威文档：`docs/部署运维/R002跨Broker公网迁移.md`（cutover boundary A/B/C 不变）。
> 本文件是 Vehicle Edge 工程化后的命令映射。

```text
A. 旧链路仍在线（cutover 前）
   1. root-only legacy backup（/root/firebot-legacy-backup-<日期>/，chmod 700）
   2. firebotctl vehicle edge-init --runtime-user tl
   3. 放 bootstrap 两个文件：
      /home/tl/firebot-edge/config/bootstrap.env  → FIREBOT_SERVER_URL=https://firebot.youdaozd.com
      /home/tl/firebot-edge/certs/mqtt-ca.crt     → 服务器下发 U-Dao private CA
   4. firebotctl vehicle readonly-preflight --phase bootstrap \
        --device-id firebot-vehicle-02 --expected-sha <FINAL_SHA>
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

只读观测：平台 check_real_vehicle_readonly.py；
REAL_ROBOT_READONLY_READY=YES 前禁止任何控制步骤。
```

- 旧 firebot-bridge.service 保留 disabled/inactive 作为回滚 artifact；
  migration-stop-legacy 停的是 firebot-bridge.service。
- 迁移期间 TAILSCALE=ROLLBACK_CHANNEL_ONLY。
