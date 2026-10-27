# U-Dao 网络与端口规划

## 1. 网络概览（当前 V1，全部 BOOTSTRAP_LOCAL_ONLY 验收态）

```
Internet（本阶段全部 OWNER_ACTION，未配置）
   │
腾讯云 VPC 172.17.0.8/20（eth0，公网出口 124.223.220.182）
   │
/srv/u-dao（vdb 200GiB）
   ├─ Docker 网络：docker0 10.239.0.1/24；compose default-address-pools 10.240.0.0/16 size=/24
   │    （与 VPC 172.17.0.0/20 无重叠；安装脚本实测复核后才写入 daemon.json）
   ├─ platform ingress（project u-dao-ingress，网络 u-dao-ingress）
   │    127.0.0.1:18000 http / 127.0.0.1:18443 https（bootstrap 证书）
   └─ firebot（project u-dao-fireboot？→ u-dao-firebot，网络 firebot + 外部 u-dao-ingress）
        edge 别名 firebot-edge（变量式 proxy_pass 运行时解析）
```

- firebot edge 以别名 `firebot-edge` 加入 `u-dao-ingress` 网络；shared ingress 用**变量式 proxy_pass + Docker DNS（resolver 127.0.0.11, TTL 10s）**路由——edge 未就绪时 502 不崩溃，重建后自动恢复（R2-10，故障注入 C1 验证）。
- shared ingress 是 **platform 资源**（独立 project/生命周期），Firebot 部署/回滚脚本禁止操作；crane-platform 后续在 conf.d/ 自加片段。
- Tailscale：**云部署不使用**。历史现场方案（8554 绑 Tailscale、firebot.lan 局域网入口）仅存在于旧现场文档并已标注，不适用于 U-Dao。

## 2. 宿主端口清单（云，全部绑定 127.0.0.1）

| 宿主端口 | 协议 | 归属 | 容器目标 | 用途 |
|---|---|---|---|---|
| 22 | tcp | 系统 sshd | — | SSH（腾讯云安全组控制） |
| 18000 | tcp | shared ingress | 18000 | 验收 HTTP（仅 health/redirect） |
| 18443 | tcp | shared ingress | 18443 | 验收 HTTPS（bootstrap 证书；COOKIE_SECURE 登录/HMI/媒体） |
| 18080 | tcp | firebot edge（调试） | 80 | edge 直连调试（对外入口是 ingress） |
| 18883 | tcp | firebot mosquitto | 8883 | MQTT over TLS 验收（bootstrap CA） |
| 18554 | tcp | firebot mediamtx | 8554 | RTSP 发布（本地测试） |
| 18890 | udp | firebot mediamtx | 8890 | SRT 发布（合成 E2E） |
| 8189 | udp+tcp | firebot mediamtx | 8189 | WebRTC ICE（**端口保持**，公网规划亦以 8189 为 canonical） |

容器内不映射宿主：5432（postgres）、6379（redis）、8889（mediamtx webrtc 内部 upstream）、9997/9998（mediamtx api/metrics）、8000（api）、8090（media-test-source 探针）。

公网动作（安全组/DNS/证书）**本阶段一律未改**：`TENCENT_SECURITY_GROUP_CHANGED=NO`。

## 3. 公网规划（OWNER_ACTION_REQUIRED，全部后置）

| 项 | 端口/协议 | 说明 |
|---|---|---|
| 正式域名 + DNS | 53 | A/AAAA 记录（如 firebot.<domain>） |
| 正式公网 TLS | 443/tcp | 替换 bootstrap 证书；shared ingress 生产 server 块（模板内注释提供） |
| Web HTTP 入口 | 80/443/tcp | 由 platform ingress 统一开放 |
| 车端 MQTT 入口 | 8883/tcp | 车端凭证需 per-robot ACL；正式 MQTT 证书单独签发（不复用 bootstrap cert） |
| 车端视频上云 | 8890/udp | SRT：鉴权 token + 独立加密 passphrase（两者禁止复用）；per-robot/per-stream 发布凭据需评估（bootstrap 全局 token 不是最终多车模型） |
| WebRTC ICE | 8189/udp（+tcp fallback） | 端口保持；公网 ICE 未验收（本阶段只做 localhost candidate 断言） |
| TURN | 3478/udp+tcp | coturn 仅有模板；公网 ICE 不达标时启用 |
| off-host 备份 | — | COS / 第二云盘（同盘备份 ≠ 灾难恢复） |
| 腾讯云安全组 | 上述端口 | 由 OWNER 一次性配置 |

未完成以上各项前：`PUBLIC_PRODUCTION_READY=NO`、`REAL_ROBOT_READY=NO`。

## 4. 云 preflight 端口语义

- Firebot 自身端口（18883/18554/18890/18080/8189）必须空闲（UPDATE_MODE 豁免 Firebot 自有）。
- 80/443 不要求永久空闲：若存在 listener，**owner 必须为 u-dao-ingress project**（`docker compose -p u-dao-ingress ps`），否则 FAIL——既防 Firebot 抢占，也兼容平台后续合法开放 80/443。
- 静态侧由 `check_server_surface.py --mode u-dao` + `test_compose_render.py` 锁定（无 0.0.0.0 绑定、无 443、无 5432/6379/9997/8889 映射）。
