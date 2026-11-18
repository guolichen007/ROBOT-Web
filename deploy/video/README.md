# 车端视频发布（已并入 Vehicle Edge 媒体组件）

> 本目录原为车端 CC 草稿（`feature/r001-v1-readonly-g1@1c4bf74`：`scripts/firebot_video_publisher.sh`
> + `firebot-video-{rgb,thermal}.service` + `env.template`）。G4-Fleet-R2 独立审查确认 10 项缺陷
> 并在实际运行中闭环后，**草稿脚本与 unit 已移除**，唯一正式实现为：
>
> - 发布器：`integration/ros1/vehicle-edge/media/firebot_media/`
> - systemd：`integration/ros1/vehicle-edge/media/systemd/firebot-media@.service.template`
> - Hardware Profile：`integration/ros1/vehicle-edge/media/profiles/firebot_ros1_standard_v1.media.env`
> - 生命周期：`firebotctl vehicle media install|credentials|preflight|start|status|stop|rollback|doctor`
> - SOP：`docs/部署运维/车端接入标准SOP.md`

## 草稿缺陷与处置（均有运行实证，见 `integration/g4-fleet-media-e2e/` 与 `media/tests/`）

| # | 草稿缺陷 | 处置 |
| --- | --- | --- |
| 1 | RGB/thermal 共用 `srt_streamid` 文件 → 同一发布路径 | streamid 进程内按 `<vehicle_id>-<camera>` 生成；token 为单车派生，路径按相机独立 |
| 2 | `wait -n … \|\| true` 使退出码恒为 0，`Restart=on-failure` 失效 | Python 主循环；可恢复故障进程内退避；配置错误 78（不重启）；崩溃非 0（Restart=always） |
| 3 | 默认 UVC 直采与局域网 ROS provider（camera_sdk/svusb_thermal）互斥 | 默认 `http_mjpeg` 复用 web_video_server：单次采集、局域网与云多消费者；v4l2 改为可选 |
| 4 | `0600 root` 凭证与低权限用户读取冲突 | 凭证落位 `media/secrets/`，`0600 <媒体用户>`；不可读即 CONFIG_ERROR（非静默） |
| 5 | 默认 `/dev/video0` `/dev/video1` 不稳定 | v4l2 只接受 `/dev/v4l/by-id|by-path`，`/dev/videoN` 拒绝 |
| 6 | RTSP 凭证进 argv（`rtsp://$URL`）且 FFmpeg/SRT 日志可能带凭证 | RTSP URL 经 0600 ffconcat 文件；日志统一脱敏 + 限速；无文件日志 |
| 7 | RTSP 未验证实际编码即 `-c copy` | ffprobe 实测，仅 H.264+yuv420p 才 copy，否则转码 |
| 8 | FFmpeg 4.2.7 不支持 `-preset p1 -tune ll`；`SRT_ENV_*` 在 srt-live-transmit 1.4.0 中不存在 | 4.2 可用参数 `-preset llhp -zerolatency 1`；SRT 改为进程内 libsrt（streamid/passphrase 只写 socket） |
| 9 | StartLimit 耗尽后长期断网不自愈 | 进程内退避不退出；`StartLimitIntervalSec=0` |
| 10 | SRT 文件日志无限额 | 无文件日志；进程 30 行/分钟 + journald 限速 |

**额外发现（运行实证）**：Ubuntu 20.04 发行版 libsrt/srt-tools 为 1.4.0，MediaMTX 1.18（gosrt）要求
对端 ≥ 1.4.1，握手必被 `REJ_VERSION` 拒绝——草稿即使修好其他问题也**无法**推流。媒体组件随 release
携带受控构建的 libsrt 1.4.4（Ubuntu 20.04 工具链编译），见 SOP。

## 局域网接口事实（草稿 README 的有效部分，经 `车端局域网web/autocar-main` 源码复核）

- web_video_server :8080，`http://<车端>:8080/stream?topic=<话题>&type=mjpeg`（MJPEG multipart）。
- RGB `/camera/color/image_raw` 由 **realsense2_camera**（单目彩色）发布——**不是**草稿所写
  camera_sdk 双目 `/dev/video0` 1280x480 拼接（camera_sdk 发布的是 `/camera/left|right/image_raw`）。
- thermal `/thermal/image`（rgb8 伪彩）由 svusb_thermal 经厂商 SDK 按 `device_index` 打开。
- rosbridge :9090、web_control `/web_control/task`：媒体组件**不触碰**。
- 实际分辨率/帧率/硬件编码可用性以 001/002 现场只读取证为准（`firebotctl vehicle media doctor`）。
