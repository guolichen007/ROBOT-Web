# 车端视频发布服务（R001/R002 同型通用，非 001 专用）

> G4-Fleet-A 交付材料：局域网接口清单、视频源统一结论、R002 同型 Hardware Profile、
> 配置模板。发布器脚本：`scripts/firebot_video_publisher.sh`（rgb|thermal 独立进程）。

## 1. 视频源统一结论

- 同型车（R001/R002）相机均为 **USB UVC**：RGB=双目模组（左右拼接 1280x480@30，
  /dev/video0）、热成像=SV 机芯（640x480@25，/dev/video1）。旧 ROS 采集
  provider：`camera_sdk`（/camera/color/image_raw）、`svusb_thermal`（/thermal/image），
  局域网观看经 web_video_server :8080（MJPEG）。
- V1 云端合同假设 **Camera RTSP** → 车端 FFmpeg 转封装 → SRT 8890 → MediaMTX。
  实车是 UVC（非 RTSP），因此发布器输入为 **V4L2 直采 + NVENC 编码**（H.264+yuv420p
  合同不变）；若相机换 RTSP 网络机，env 里填 `*_RTSP_URL` 即自动走 `-c copy` 转封装。
- **设备占用铁律**：同一台车内，若 ROS 采集节点（camera_sdk / svusb_thermal）已在运行
  （持有 /dev/videoN），发布器**不得**直开同一设备；发布器启动会做输入可用性检测，
  打不开设备即 fail-closed 退出（systemd 有界退避），不双开同一 USB 相机。
- 云端发布器与 ROS provider 是**两种取流路径**，同型车默认由发布器独立取流
  （UVC 直开）；仅当车内已有 ROS 采集节点时切换为复用其 image 话题（后续需要时再增）。

## 2. 服务运行架构（同型车标准）

```text
浏览器 web_nav（车端 http 静态托管）── ws://<车端>:9090 ──> rosbridge_server ──> roscore
web_video_server :8080（MJPEG 局域网观看：/camera/color/image_raw、/thermal/image）
web_control /web_control/task（ROS service，任务白名单，经 rosbridge 调用）
云端链路（V1）：相机 → firebot_video_publisher（ffmpeg→UDP 回环→srt-live-transmit）
  → SRT 8890/udp → MediaMTX → WHEP/WebRTC 8189 → Web HMI
```

## 3. R002 同型 Hardware Profile（供桌面开发 CC）

| 项 | 值 |
|---|---|
| 工控机 | x86_64，12 核 / 31G / NVMe 468G；Ubuntu 20.04 + ROS Noetic |
| 网卡 | eno1=I219-V、enp3s0/enp4s0=I210×2、无线 8265（管理网）、Tailscale（回滚通道） |
| 相机 | RGB 双目 USB UVC 1280x480@30（左右拼接，/dev/video0）；热成像 SV USB UVC 640x480@25（/dev/video1） |
| 硬件编码 | nvenc_h264（NVIDIA 2582）+ h264_vaapi（/dev/dri/renderD128） |
| 业务工作空间 | ~/firerobot_ws（ugv_sdk/battery_sdk/smog_detect/bbs_localizer/hipnuc_imu/rslidar_sdk/realsense2_camera/camera_sdk/svusb_thermal/navigation/web_control/web_nav 等） |
| 业务 ROS 接口 | /battery/status、/smog/status、/thermal/image、/camera/color/image_raw、/odom、/cmd_vel、/move_base/goal、/e_stop、/web_control/task 等（见任务报告矩阵） |
| 通信 Runtime | firebot-edge（firebotctl vehicle edge-*；R002 迁移流程见 docs/部署运维/R002跨Broker公网迁移.md） |

## 4. systemd 单元（有界退避 + 日志限额 + 低权限）

- `firebot-video-rgb.service` / `firebot-video-thermal.service`：各自独立进程，
  单路故障不影响另一路；Restart=on-failure、RestartSec=10s、
  StartLimitIntervalSec=300s + StartLimitBurst=6（300 秒最多 6 次，防无限高频重启）、
  LogRateLimit 200 条/30s、User=firebot-video、NoNewPrivileges、PrivateTmp。
- 安装启用前置：①两台相机物理接入 ②云端交付 SRT endpoint/streamid/token/passphrase
  （凭证投递 `/etc/firebot/video/srt_{streamid,passphrase}`，0640 root:firebot-video，
  值不进 argv/日志/聊天/Git）③创建 firebot-video 系统用户并加入 video 组。
  未授权前不向生产服务器推送。

## 5. 本机实测矩阵（2026-11-18，G4-Fleet-A2；供统一媒体模块引用）

| 能力 | 实测结果 | 结论 |
| --- | --- | --- |
| ffmpeg 4.2.7 h264_nvenc（RTX 3050，驱动 595.71.05） | `-preset p1`/`ll`/`default`/裸参数全部报 `Cannot get the preset configuration: unsupported param (12)` | **不可用**（旧 ffmpeg 与新驱动 NVENC 枚举不兼容） |
| ffmpeg 4.2.7 libx264 软编 1280x480@30 | 输出 h264+yuv420p+30fps 正常 | **可用（发布器当前默认）** |
| ffmpeg 4.2.7 h264_vaapi（renderD128，iHD/i965 驱动均在） | `vaGetDriverNameByIndex failed`（root 同样失败） | **不可用**（libva 2.7 栈不支持 12 代 iGPU） |
| h264_v4l2m2m | 无 /dev/media* 设备 | 不可用 |
| srt-live-transmit 1.4.0 数据面（回环） | ffmpeg→UDP→SRT→UDP 全管道 `SRT target connected`，传输正常 | **可用** |
| srt-live-transmit 1.4.0 URI `?passphrase=` | `failed to set options: passphrase` + #5003 | **不支持** |
| libsrt 1.4.0 SRTO_PASSPHRASE setsockopt | rc=-1（LATENCY/STREAMID/PBKEYLEN 对照均 rc=0，加密后端 libcrypto+HaiCrypt 链接完好） | **加密选项设置失败** |
| libsrt 1.4.0 `SRT_ENV_PASSPHRASE/STREAMID` 注入 | getsockflag 读回空（1.4.0 < 引入该特性的 1.4.4） | **不生效，凭证会静默失效** |
| srt-live-transmit 断连行为 | 默认 autoreconnect=yes 无限重连不退出；`-a:no` 断开即退出（rc=0） | 发布器已改 `-a:no` + rc=0→1 转失败退出码，触发 systemd 有界退避 |
| 凭证文件 0600 root:firebot-video | firebot-video 读被拒（`权限不够`） | **必须 0640 root:firebot-video**（或 0600 firebot-video:firebot-video） |
| 进程 environ 凭证可见性 | G1 生产进程 /proc/PID/environ=0400 firebot:firebot，非属主 tl 读取被拒 | 凭证经 env 注入不泄漏给非属主 |
| 发布器 fail-closed | 无 env/无设备/无凭证/无 endpoint 四例全部 rc=1 + 明确 FAIL 行 | 按设计 fail-closed |

> 统一媒体模块落地前必须解决：libsrt 升级 ≥1.4.4（推荐 1.5.x）以恢复
> SRT_ENV_PASSPHRASE/STREAMID 凭证注入，或改用其他不进 argv 的凭证投递方式；
> 编码默认 libx264，NVENC/VAAPI 需升级 ffmpeg/驱动栈后启用。
