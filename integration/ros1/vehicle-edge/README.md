# Vehicle Edge（车端通信 Runtime）

> VEHICLE_EDGE_HOME_ROOT_CONSOLIDATION 的唯一正式实现。
> 本目录是车辆↔云通信层的**唯一 source of truth**；旧 `integration/ros1/vehicle-bridge/`
> 与顶层 readonly/migration 脚本已降级为 thin shim（NO_DUPLICATED_PRODUCTION_LOGIC）。

## 工程边界（铁律）

```text
/home/tl/ROBOT-Web       Git 源码/交付源（本仓库）
/home/tl/firebot-edge    车辆 ↔ 云通信 Runtime（canonical：FIREBOT_EDGE_HOME）
/home/tl/firerobot_ws    ROS / 底盘 / 导航 / 控制 Runtime（本模块绝不触碰）
```

- **firebot-edge != ROS workspace**；Control（firebot_control/cmd_vel/navigation/
  move_base/base driver）明确不进入本模块。
- 服务器 Fleet 生命周期脚本（fleet-sites/fleet-preregister/fleet-register）留在
  `integration/ros1/`，不搬入本目录。
- 车辆只是数据（DEVICE_ID/DISPLAY_NAME/SITE_CODE/PROFILE_ID）；禁止 R002/R003
  目录、`if vehicle_id == "R002"` 等车辆特定生产代码。

## Edge Home 布局（/home/tl/firebot-edge）

```text
├── bin/                  firebot-edge 主 CLI（run/status/watch/selftest/doctor/version/logs）
├── releases/<SHA>/       immutable release（VERSION + manifest.json + bin/bridge/lib/monitor）
├── current -> releases/<SHA>     root-only 原子切换；previous -> releases/<PREV>
├── config/               install.env / identity.env / bootstrap.env / assignment.env /
│                         bridge.env / profile.env（root:root 0644，运行进程只读）
├── certs/mqtt-ca.crt     U-Dao private CA（0644，公开信任材料）
├── secrets/              mqtt.env / device-token / media-secret.env（root:root 0700/0600）
├── runtime/              status.json（canonical，FIREBOT_BRIDGE_STATUS_FILE）
├── data/                 connection-state.json（运行用户 0750）
├── logs/                 connection.log / audit.log（运行用户 0750；轮转 10MB×5/×10）
├── backups/              legacy/ releases/（root:root 0700，可能含 secret）
├── tools/                管理平面（management plane）release domain（root:root 0755）
│   ├── releases/<SHA>/   firebotctl + vehicle-edge/{lifecycle,install,systemd,config,lib,bin,bridge,monitor}
│   ├── current -> releases/<SHA>    root-only 原子切换；previous -> releases/<PREV>
│   └── bootstrap/        （预留）
└── manifest.json         非 secret；lifecycle 自动生成，禁止人工维护
```

## 双 release 平面（数据平面 / 管理平面）

```text
数据平面  /home/tl/firebot-edge/releases/<SHA>     firebot-vehicle-edge 运行时
管理平面  /home/tl/firebot-edge/tools/releases/<SHA> firebotctl + lifecycle/install/systemd/config
```

两者独立 current/previous、完整性验证、回滚。升级 CLI 绝不自动改变运行中的数据平面版本。
生产 firebotctl = `/usr/local/bin/firebotctl -> /home/tl/firebot-edge/tools/current/firebotctl`；
生产模式（VEHICLE_TOOLS_RELEASE）禁止 Git 依赖、禁止 server/fleet scope。

## 正式命令（firebotctl 分发）

```text
服务器：fleet preregister <ID> --name N --site-code S   →  fleet register <ID>（现场即时 token）
车端：
  edge-init --runtime-user tl             目录/权限初始化（显式用户；禁止 id -un 自动猜）
  readonly-preflight --phase bootstrap    接入前检查（旧链路仍在线允许）
  migration-stop-legacy                   cutover 唯一切换点（停 legacy firebot-bridge）
  enroll <ID> --token <T>                 Enrollment 输出到 Edge Home（token 零消费门）
  edge-install --sha <SHA>                通信专用安装（不碰 ROS workspace；安装≠启动）
  edge-verify --sha <SHA>                 release/manifest/权限/service inactive 验证
  readonly-preflight --phase enrolled     EXACT_SHA_MATCH 强制 + SUPPORTED_COMMANDS 空
  readonly-start                          唯一允许启动 firebot-vehicle-edge 的入口
  edge-rollback                           通信迁移唯一回滚入口（恢复 legacy 通信 owner）
  tooling-status                          管理平面 tools release 只读状态
  version / --version                     只读自检（布局/SHA）
  vehicle-connection-status / -watch / connectivity-selftest / edge-doctor
```

## 关键语义

- `edge-install ≠ start`：安装后 firebot-vehicle-edge inactive+disabled。
- **LEGACY_AND_EDGE_MUTUAL_EXCLUSION**：firebot-bridge 与 firebot-vehicle-edge
  不得同时成为 communication owner（systemd Conflicts + 脚本双 gate）。
- `readonly-start` 启动后验收失败自动 `disable --now`（FAIL_CLOSED_BRIDGE_STOP），
  不留下半在线 Bridge。
- 状态模型：HEALTHY(0)/DEGRADED(10)/OFFLINE(20)/UNSAFE(30)/CONFIG_ERROR(40)/
  INTERNAL_ERROR(50)；selftest/watch 只读本地 status.json，绝不新建第二 MQTT
  session、不发 publish/subscribe probe。
- 回滚：新链路失败 → `firebotctl vehicle edge-rollback`（唯一通信迁移回滚入口）：
  Control 安全门 → legacy unit 存在 → legacy bridge.env 仍指 100.110.31.112
  （漂移 → LEGACY_CONFIG_DRIFTED / MANUAL_RESTORE_REQUIRED=YES）→
  `disable --now firebot-vehicle-edge` → `enable --now firebot-bridge` →
  验证 MQTT_HOST=100.110.31.112 + MQTT_CONNECTED=YES；幂等（A/B/C/D）。
  与 `firebotctl vehicle rollback`（旧整车安装回滚）严格区分。

详见 `docs/`（新车辆接入 / 现场操作 / 连接状态说明 / R002跨Broker迁移 / 故障排查 / 回滚）。
