# 车端接入标准 SOP（唯一正式版本）

> 适用：同型 Firebot 车辆（ROS1 Noetic / Ubuntu 20.04；R001/R002 同型）。
> 本文是**车端接入的唯一正式 SOP**。旧文档（`integration/ros1/vehicle-edge/docs/新车辆接入.md`、
> `deploy/video/README.md`、`docs/部署运维/车端部署与实车接口.md` 中的接入段落）只保留背景与指向本文的链接。
> 车辆只是数据：`DEVICE_ID / DISPLAY_NAME / SITE_CODE / Transport Profile / Hardware Profile`。
> 禁止复制车辆目录、改源码、手工编辑 MQTT 地址/密码/systemd，禁止在命令行或聊天中传递任何凭证。

## 0. 名词与边界

| 名词 | 含义 | 唯一来源 |
| --- | --- | --- |
| Transport Profile | MQTT 接入参数集合：host / port / protocol / CA / broker_ref / adapter / credential provisioner | `deploy/u-dao/transport-profiles.json`（OWNER 批准随版本发布；解析器 `deploy/u-dao/scripts/transport_profile.py`） |
| Hardware Profile | 车内视频源事实：provider（默认 web_video_server）、话题、帧率、码率 | `integration/ros1/vehicle-edge/media/profiles/<profile>.media.env` |
| Edge Home | 车端通信 Runtime 根（`/home/tl/firebot-edge`） | `firebotctl vehicle edge-init` |
| 媒体组件 | 一路相机一个进程 `firebot-media@<camera>`，H.264 → SRT → MediaMTX | `integration/ros1/vehicle-edge/media/` |

铁律：安装 ≠ 启动；`CONTROL_ENABLED=NO / CONTROL_ACTIVE=NO / SUPPORTED_COMMANDS=（空）` 全程保持；
媒体组件不新增任何运动控制路径，不停止、不重启 G1 readonly agent / Vehicle Edge / ROS 业务栈。

## 1. Transport Profile 配置（服务器，OWNER）

| Profile | Broker | 端口 | 协议 | 平台凭证签发 | 可分配新车 | 当前车辆 |
| --- | --- | --- | --- | --- | --- | --- |
| `u-dao-standard-v1` | U-Dao 主 mosquitto | 8883 | canonical-mqtt-1.3 | `60-onboard-vehicle.sh`（硬绑定） | 是 | firebot-vehicle-02，新车默认 |
| `r001-independent-v1` | r001-broker（独立） | 8884 | vehicle-agent-v1 | 无（G1 独立签发） | 否 | firebot-vehicle-01 |

- 解析规则 fail-closed：已映射车辆不得改用其他 Profile（`PROFILE_CONFLICT`）；未映射新车必须显式
  `--transport-profile`（`PROFILE_REQUIRED`）；host 只来自 `public-activation.env`，私网/Tailscale/占位拒绝；
  无 provisioner 的 Profile 拒绝签发（`NO_PROVISIONER`）；provisioner 与 Broker/命令/协议/端口在代码中硬绑定
  （`BROKER_MISMATCH`）。**不存在任何默认端口。**
- 新增/修改 Profile：改 `transport-profiles.json` → 跑 `python3 scripts/test_transport_profile.py` → OWNER 批准 → 发布。
- `protected_vehicles`（001、002）：通用媒体登记入口拒绝；002 重跑 `fleet register` 需 `--confirm-protected`。

## 2. Hardware Profile 配置（同型车）

默认 `firebot_ros1_standard_v1`：两路均 `provider=http_mjpeg`，复用车内 web_video_server :8080：

| camera | 话题 | 发布者 |
| --- | --- | --- |
| `roof_rgb` | `/camera/color/image_raw` | realsense2_camera（单目彩色） |
| `roof_thermal` | `/thermal/image` | svusb_thermal（rgb8 伪彩） |

单次采集、多消费者：相机只被 ROS 驱动打开一次，局域网 web_nav 与云端发布器都是 web_video_server 的 HTTP 消费者。
可选 provider（非同型默认，启用前确认无 ROS 节点持有设备）：`v4l2`（只接受 `/dev/v4l/by-id|by-path`，拒绝 `/dev/videoN`）、
`rtsp`（URL 含凭证 → `media/secrets/rtsp_<camera>.url` 0600；实测 H.264+yuv420p 才转封装，否则转码）。
现场差异（端口、帧率、码率）写入 `config/media-hardware.env`，install 不会覆盖已改动的现场值（`KEPT_LOCAL`）。

## 3. 新车接入（firebot-vehicle-NN）

```text
服务器（管理员）
  firebotctl fleet preregister firebot-vehicle-03 --name firebot-003 --site-code <SITE>
  firebotctl fleet register firebot-vehicle-03 --transport-profile u-dao-standard-v1
      → TRANSPORT_PROFILE=… / MOSQUITTO_ZERO_RESTART=PASS / VEHICLE_ACL_LIVE=PASS
      → ENROLL_TOKEN_FILE=<runtime>/secrets/vehicles/firebot-vehicle-03/enroll-token（0600，不回显）
  经安全通道把 token 文件交给现场（不贴聊天/工单）
  python -m app.dev.register_vehicle_streams generic --vehicle-id firebot-vehicle-03 \
      --transport-profile u-dao-standard-v1            # 预演
  … --apply > receipt-firebot-vehicle-03.json           # 登记两路流（保存 receipt）

车端（root，按顺序）
  firebotctl vehicle edge-init --runtime-user tl
  # 放置 config/bootstrap.env（FIREBOT_SERVER_URL=https://firebot.youdaozd.com）与 certs/mqtt-ca.crt
  firebotctl vehicle readonly-preflight --phase bootstrap --device-id firebot-vehicle-03 --expected-sha <SHA>
  firebotctl vehicle enroll firebot-vehicle-03 --token-file <token 文件>    # 或 --token-stdin（不回显）
      → TRANSPORT_PROFILE=u-dao-standard-v1:firebot.youdaozd.com:8883 / ENROLL=PASS / MEDIA_CREDENTIALS=RECEIVED
  firebotctl vehicle edge-install --sha <SHA>
  firebotctl vehicle edge-verify --sha <SHA>
  firebotctl vehicle readonly-preflight --phase enrolled --expected-sha <SHA>
  firebotctl vehicle readonly-start                       → READONLY_BRIDGE_START=PASS
  firebotctl vehicle media install --sha <SHA>            → MEDIA_INSTALL=PASS（inactive）
  firebotctl vehicle media credentials import --from-enrollment
  firebotctl vehicle media preflight                      → MEDIA_PREFLIGHT=PASS（libsrt ≥1.4.1、编码器实测）
  firebotctl vehicle media start                          → firebot-media@roof_rgb / @roof_thermal
  firebotctl vehicle media status                         → state=LIVE stream_id=firebot-vehicle-03-roof_*
```

`--token <值>` 会被拒绝（`TOKEN_ON_ARGV_FORBIDDEN`）。token 一次性；重放 401。服务器重复 `fleet register`
不旋转 MQTT 凭证，且车上正在使用的 device token 在新 token 被 activate 之前一直有效。

## 4. 已有车辆媒体增量补齐（以 001 为例）

001 使用 `r001-independent-v1`（8884），G1 readonly agent 已在运行：**不重新 enroll、不生成/轮换 MQTT 密码、不停止 G1**。

```text
服务器
  bash deploy/u-dao/scripts/60-onboard-vehicle.sh firebot-vehicle-01 --media-only
      → media-bundle.json（0600，单车 token + 平台共享 passphrase；无主密钥；不碰 MQTT / 不 reload Broker）
  （fleet register firebot-vehicle-01 → NO_PROVISIONER 拒绝；--apply/--force 对 001 同样拒绝）
  python -m app.dev.register_vehicle_streams            # 001 专项入口：预演
  python -m app.dev.register_vehicle_streams --apply > receipt-001.json

车端（root）
  firebotctl vehicle edge-init --runtime-user tl          # 只建目录，不 enroll
  firebotctl vehicle media install --sha <SHA>            → MEDIA_PROTECTED_UNITS_UNCHANGED=PASS
  firebotctl vehicle media credentials import --vehicle-id firebot-vehicle-01 \
      --srt-host <SRT 公网主机> --srt-port <端口> --bundle-file media-bundle.json
  firebotctl vehicle media preflight && firebotctl vehicle media start
```

002 同理（Profile `u-dao-standard-v1`；若已 enroll 新版可用 `--from-enrollment`，否则同 001 用 `--media-only`）。
受保护车辆的流登记由 OWNER 审批后通过专项流程执行，通用入口拒绝。

## 4A. 中断续跑与受控恢复

| 中断点 | 状态 | 续跑方式（全部幂等，可原样重跑同一命令） |
| --- | --- | --- |
| `fleet preregister` 中断 | Robot 行单事务：要么不存在，要么完整 | 重跑；完全一致 → `EXISTS`，不一致 → `CONFLICT`（人工核对，不自动改） |
| `fleet register` 凭证准备后、token 签发前中断 | MQTT 凭证已在 Broker；无 token 文件、无临时文件 | 重跑同一命令：`*_EXISTING_CREDENTIAL_REUSED=PASS`（不旋转），重新签发 token |
| 媒体登记（`register_vehicle_streams`）中断 | 单事务：零行或全部行 | 重跑 `--apply`：已存在 → `REUSE`，缺失 → `CREATE`；receipt 以最后一次成功输出为准 |
| `media install` 中断 | staging 删除，`current` 不变 | 重跑；同 SHA 已完整 → `EXISTS_VERIFIED`；残缺 → 拒绝覆盖，人工删除该 release 后重跑 |
| `media credentials import` 中断 | 临时文件在 0700 目录内，原子 rename | 重跑 |

**Enrollment 已消费、但 HTTP 响应丢失**（车端无 `identity.env` / `mqtt.env`，或 `ENROLL=FAIL（API 不可达）` 而服务器侧 token 已消费）：

1. 车端确认未写入：`config/identity.env`、`secrets/mqtt.env` 不存在（enroll 在写盘前完成全部校验，写盘为原子 rename）。
2. 不要重试旧 token（已消费 → 401；签发记录在消费时已清除，凭证不会再次下发）。
3. 服务器重新签发：`firebotctl fleet register <ID> --transport-profile <同一 Profile>`（受保护车辆加
   `--confirm-protected`）。MQTT 凭证 ensure 语义不旋转；生成新的一次性 token 文件。
4. 车端用新 token 文件重新 `vehicle enroll`。丢失响应里的 device token 虽已在服务器生效，但车端从未持有；
   新 activate 成功时以新 device token 替换，旧值随之作废。
5. 校验：`readonly-preflight --phase enrolled` PASS。整个过程无需人工改 DB、无需轮换 MQTT 密码。

## 4B. 现役 001 的身份一致性

- G1 readonly agent 的唯一身份源：`/etc/firebot/vehicle-agent-v1-readonly/device_identity.json` 的 `vehicle_id`
  （MQTT username == vehicle_id）。
- `media credentials import` 与 `media preflight` 由 root 读取该文件的 `vehicle_id`（不读取、不输出凭证字段），
  与媒体 bundle 的 `vehicle_id` 不一致 → 拒绝；一致 → `MEDIA_IDENTITY_MATCHES_G1=YES`。
- 云端媒体流 `firebot-vehicle-01-roof_*` 归属同一 Robot 行（`vehicle_id=firebot-vehicle-01`），由 001 专项登记入口写入。

## 4C. 「G1 进程 active」≠「MQTT 在线」

| 观测 | 含义 | 处置 |
| --- | --- | --- |
| `systemctl is-active firebot-vehicle-agent-v1-readonly` = active | 进程在运行（会在断网期间按退避自动重连） | 不代表云端在线 |
| Web/API `online_state=OFFLINE` 且 `availability=offline` | 云端收到 broker LWT 或 graceful offline：车端与 Broker 的 MQTT 会话已断 | 查车端网络 / Broker 可达性；**不要**重启或重新 enroll G1 |
| 网络恢复后 | Agent 同进程重连 → 新会话（新 boot_id）→ ONLINE | 只读确认 `last_seen_at` 刷新 |

媒体组件与 G1 完全独立：断网期间 `media status` 显示 `NETWORK_DOWN`，网络恢复同进程自愈；G1 的 active 状态
既不触发也不阻止媒体发布。验收时两者分别判定，不得用 `is-active` 代替在线判定。

## 5. 安装 / 启动 / 诊断 / 升级 / 回滚

| 动作 | 命令 | 语义 |
| --- | --- | --- |
| 安装 | `media install --sha <SHA> [--libsrt-source <tar.gz> \| --libsrt-prebuilt <dir>]` | 精确 SHA + 干净仓库；libsrt 1.4.4 三选一：在线车端编译 / 离线源码包（`build-libsrt.sh` 校验源码 SHA256）/ 离线预构建（必须与仓库发布 Manifest `media/release/libsrt-artifacts.json` 的 sha256 完全一致，离线包自带 `.sha256` 不作信任根）；release 自检后原子切换；失败 staging 清理、current 不变 |
| 凭证 | `media credentials import …` | 只接受 0600 bundle 文件 / stdin；错误身份、含主密钥字段、argv 传值一律拒绝 |
| 预检 | `media preflight` | 配置、凭证权限、libsrt 版本、编码器 1 帧实测、媒体身份 = Enrollment 身份 |
| 启动 | `media start [camera\|all]` | 预检通过才 enable --now；受保护单元前后快照一致 |
| 停止 | `media stop [camera\|all]` | disable --now 并确认 inactive |
| 状态 | `media status` / `media doctor` | 每路 state：LIVE / SOURCE_UNAVAILABLE / SOURCE_STALLED / DEVICE_BUSY / NETWORK_DOWN / AUTH_REJECTED / CONFIG_ERROR |
| 升级 | `media install --sha <新SHA>` 后 `media start` | previous 保留 |
| 回滚 | `media rollback` | current ← previous（校验 MANIFEST）；服务保持停止，确认后 `media start` |

运行语义：可恢复故障（相机缺失、断网、云端拒绝）在进程内指数退避自愈，**不依赖 systemd 重启**；
永久配置错误退出 78 不再重启；`StartLimitIntervalSec=0`；日志只进 journald（进程 30 行/分钟 + journald 限速），全部脱敏。

## 6. 真实硬件验收（FIELD_GATE，必须现场执行）

隔离环境已证明软件链路（见 `integration/g4-fleet-media-e2e/`，视频源为测试图案，**不计为硬件证据**）。现场逐项：

1. `media doctor`：web_video_server 两路话题有帧；记录实际分辨率/帧率；`/dev/video*` 持有者为 ROS 节点（发布器未直开）。
2. `media preflight`：libsrt=1.4.4；编码器（nvenc/vaapi/libx264）实测结果。
3. `media start` 后 Web 监控页两路 VideoCard 出画，局域网 web_nav 同时出画。
4. 拔掉 thermal USB 30s：thermal → SOURCE_STALLED/UNAVAILABLE，RGB 不受影响；插回自动恢复。
5. 断开车端上行 ≥ 5 分钟：NETWORK_DOWN；恢复后同进程自愈（`media status` 的 reconnects 增加，pid 不变）。
6. G1 readonly agent / Vehicle Edge 在整个过程中 active 不变。

## 7. 退役与凭证撤销边界

| 对象 | 撤销方式 | 影响范围 |
| --- | --- | --- |
| 媒体发布 | 车端 `media stop` → 服务器 `register_vehicle_streams … --rollback receipt --apply`（活动会话存在则拒绝） | 仅该车两路流 |
| 单车 SRT token | 确定性派生，无法单独吊销：撤销登记即拒绝发布；需更换 → 旋转主密钥后对所有车 `--resync-srt` 并重新下发 bundle | 全车队 |
| 平台 SRT passphrase | 全车队共享（链路加密）；旋转需全车队同步 | 全车队 |
| MQTT 凭证 | 按 Profile 的 provisioner（标准：`60-onboard --force` 旋转 + SIGHUP）；001 由 G1 独立流程 | 单车 |
| Enrollment token | 一次性、1h 过期，消费即清除签发记录；过期记录在下次签发时清理 | 单车 |
| device token | 新 token 仅在 activate 成功时替换 | 单车 |

已知限制：SRT streamid（含单车 token）在握手中以明文传输（MediaMTX 协议设计），passphrase 只加密载荷。
在 per-vehicle passphrase 或短期 token 落地前，按上表在疑似泄露时执行全车队轮换。

## 8. 证据与测试入口

- 发布器单测：`python3 integration/ros1/vehicle-edge/media/tests/test_media_publisher.py`
- 生命周期：`python3 integration/ros1/vehicle-edge/media/tests/test_media_lifecycle.py`
- Transport Profile / fleet register：`python3 scripts/test_transport_profile.py`
- enroll：`python3 integration/ros1/vehicle-edge/tests/test_enroll_transport.py`
- 真实 systemd 语义：`python3 integration/ros1/vehicle-edge/tests/test_real_systemd_semantics.py`
- 隔离标准化验收（新车 / 001 增量 / 负测 / 长断网）：`bash integration/g4-fleet-media-e2e/run_all.sh`

## 9. 实际安装命令（工控机，root；以封板 SHA 为准）

```text
# 0) 交付源：ROBOT-Web 检出到封板 SHA（clean）
cd /home/tl/ROBOT-Web && git fetch origin && git checkout --detach <SEAL_SHA> && git status --porcelain   # 必须为空
# 1) 依赖（Ubuntu 20.04）
apt-get install -y python3 ffmpeg            # 在线编译 libsrt 时另需：cmake build-essential libssl-dev
# 2) libsrt 1.4.4 —— 离线预构建包（来自交付目录 libsrt-1.4.4-focal/，按发布 Manifest 校验）
firebotctl vehicle media install --sha <SEAL_SHA> --libsrt-prebuilt /path/to/libsrt-1.4.4-focal
#    或离线源码包：--libsrt-source /path/to/srt-1.4.4.tar.gz（SHA256 93f5f371…094114）
# 3) 001：导入媒体 bundle（服务器 60-onboard --media-only 产出，0600 经安全通道传输）
firebotctl vehicle media credentials import --vehicle-id firebot-vehicle-01 \
    --srt-host <SRT 公网主机> --srt-port <端口> --bundle-file /root/media-bundle.json
shred -u /root/media-bundle.json
# 4) 预检 → 启动 → 状态
firebotctl vehicle media preflight
firebotctl vehicle media start
firebotctl vehicle media status
```
