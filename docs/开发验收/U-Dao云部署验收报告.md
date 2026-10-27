# U-Dao 云部署验收报告（Cloud Deployment V1）

> 验收日期：2026-10-27。服务器：腾讯云 U-Dao（ap-shanghai，ins-j2y7kw5p，Ubuntu 22.04.5，8C/15G）。
> 部署支线：`feature/u-dao-cloud-deployment-v1`（基线 60d30bbe…，禁止从 main 开发）。
> 状态拆分（R2-25）：本报告只证明「基础与本地云 E2E」；**公网生产就绪与真实车辆就绪均为 NO**（OWNER_ACTION_REQUIRED 未完成）。

## 1. 最终状态（结论）

```text
U_DAO_CLOUD_V1_RESULT=PASS（FOUNDATION + LOCAL_CLOUD_E2E 范围内）

FOUNDATION_READY=PASS
LOCAL_CLOUD_E2E=PASS
PUBLIC_PRODUCTION_READY=NO
REAL_ROBOT_READY=NO
REAL_ROBOT_CONNECTED=NO
REAL_ROBOT_COMMAND_SENT=NO
DOWNLINK=false
```

## 2. 版本与发布

```text
BASE_SHA=60d30bbeea71d3ab1004876fe424361225f3c027
BRANCH=feature/u-dao-cloud-deployment-v1
FINAL_HEAD_SHA=<最终 HEAD（push 后回填）>
REMOTE_PUSH=<push 后回填>
```

- immutable release：`/srv/u-dao/projects/firebot/releases/<40SHA>`（目录名==HEAD==SHA、clean、detached 断言通过）
- 运行证明三重：current.json（current=exact_sha、previous 链 b2134c3→a65d7629→ea66bd5→34a0833→55ba7e6→…）+ 容器 OCI revision 与 current.json 一致（IMAGE_SHA_MATCH=PASS）+ manifest 记录第三方镜像 RepoDigest
- fresh 状态机实测：bootstrap（previous=none）→ 首发布保持 none → 后续发布轮转 previous（test_cloud_deploy.py 16 用例 + 实测链）

## 3. 服务器底座

```text
DATA_DISK=/dev/vdb1 ext4 label=u-dao（reserved 1%，UUID 0e8372cb-22e0-448e-ad58-ce91a8a8adaf 入 fstab）
U_DAO_ROOT=/srv/u-dao（软链 /home/ubuntu/u-dao）
DOCKER_VERSION=29.9.2（官方源；major-version gate 审计基线）
DOCKER_COMPOSE_VERSION=5.6.1
DOCKER_DATA_ROOT=/srv/u-dao/platform/docker
CONTAINERD_ROOT=/srv/u-dao/platform/containerd（containerd image store 实测启用 snapshotter.v1）
DOCKER_BRIDGE=10.239.0.1/24
DEFAULT_ADDRESS_POOL=10.240.0.0/16（size /24）
VPC_OVERLAP=无（安装前路由复核 + 实测）
MOUNT_GUARD=RequiresMountsFor + ConditionPathIsMountPoint 生效（失败注入实测：数据盘未挂载时 docker/containerd 拒绝启动，journal 有 condition skipped 证据）
```

## 4. 服务与网络

```text
SHARED_INGRESS=u-dao-ingress project（nginx，127.0.0.1:18000/18443；Firebot 部署/回滚脚本不操作该项目）
FIREBOT_EDGE=HTTP-only（127.0.0.1:18080 调试；TLS 由 ingress 终结；X-Forwarded-Proto 透传）
HOST_80_PUBLIC=NO
HOST_443_PUBLIC=NO
```

服务健康（server-deploy.sh verify 全 gate）：POSTGRES=HEALTHY、REDIS=HEALTHY、MOSQUITTO=HEALTHY、MEDIAMTX=HEALTHY、API=HEALTHY、MQTT_INGRESS=HEALTHY、COMMAND_DISPATCHER=HEALTHY、TASK_WORKER=HEALTHY、WEB=HEALTHY、VEHICLE_AGENT_ADAPTER=HEALTHY（rollout=true、downlink=false）、ROS_COMPAT=OPTIONAL（ROS_COMPAT_MODE=false）。

宿主端口复核（ss）：全部 127.0.0.1（22/18000/18443/18080/18883/18554/18890/8189），无 0.0.0.0 业务端口，无 5432/6379/8889/9997 映射。

## 5. MQTT

```text
MQTT_TLS=PASS（bootstrap CA 验证的 mosquitto_pub/sub 实测；SAN 含 mosquitto/localhost/u-dao/127.0.0.1）
MQTT_PERSISTENT_SESSION=PASS（ingress 双 client：realtime clean / durable 600s；adapter 600s；B3 对照实测恢复投递）
MQTT_MESSAGE_EXPIRY=PASS（B1：过期消息不补发；发布侧注入 max(1,ceil(remaining))）
MANUAL_REPLAY_PROTECTION=PASS（B2：QoS0 断线不排队；broker queue_qos0_messages false；manual QoS0+500ms TTL 保留）
ACK_REPAIR=保留（late ACK 修复/transport_timeout 可被真实 ACK 替换，backend 测试覆盖）
```

## 6. 媒体链

```text
SRT_PIPELINE=PASS（合成 RGB/thermal 双流 srtConn 注册 ready；codec H264，yuv420p 由发布端强制）
MEDIA_AUTH=PASS（三态实测：无 token 401 / 错 passphrase 失败 / 正确凭据就绪；SRT 加密 passphrase 与鉴权 token 独立）
WHEP_PIPELINE=PASS（登录→ticket→WHEP 201→SDP answer 含 127.0.0.1:8189 ICE candidate（端口保持））
VIDEO_RECONNECT=PASS（VideoCard 有界重连 3 次退避+epoch 防污染；前端 16 用例含重连场景）
BROWSER_E2E=PASS（bootstrap HTTPS 18443：COOKIE_SECURE 登录/改密/HMI 渲染/用户点击→WHEP LIVE/真实 ICE candidate-pair succeeded）
```

## 7. 故障注入（cloud_fault_injection.sh，25/25 PASS）

A1 ingress restart 恢复；A2 adapter restart 恢复；A3 mosquitto restart 同容器内恢复（StartedAt 不变）；A4 api restart；A5 redis restart；A6 mediamtx restart；A7 mosquitto pause 10s；A8 realtime 无 backlog 回放 + durable 队列恢复 + 最新 location 直达收敛；B1 过期不补发；B2 QoS0 不排队；B3 durable 对照恢复；C1 edge force-recreate 后 ingress 无人工 reload 恢复。

## 8. 测试体系

```text
BACKEND_TESTS=PASS（pytest apps/api/tests + services/protocol-tester/tests：217 passed）
FRONTEND_TESTS=PASS（vitest 96 passed；format/lint/typecheck/build 全过）
COMPOSE_TESTS=PASS（test_compose_render：legacy 渲染回归锁 + U-Dao 云渲染断言）
MIGRATION_MATRIX=PASS（test_migration_matrix + check_migration_manifest）
STATIC_CHECKS=PASS（ruff check + ruff format --check + mypy apps/api/app services/protocol.py）
GITLEAKS=PASS（zricethezav/gitleaks detect 零发现）
CLOUD_STATE_MACHINE=PASS（test_cloud_deploy.py 16 用例：fresh/首发布/二次发布/回滚/guard/schema gate）
```

## 9. Host baseline（只读记录，本轮未改动）

```text
TIME=Asia/Shanghai，NTP synchronized=yes（ntpd active）
UFW=inactive（腾讯云安全组控制，本轮未修改）
SSH=22（密码登录+root 登录开启——上阶段只读检查记录，未修改）
SWAP=无（内存 15Gi）
DISK=/ 已用约 6G；/srv/u-dao 196G 可用
APT=腾讯内网镜像源包缺失 → 已切换 mirrors.cloud.tencent.com（备份 /etc/apt/sources.list.bak.*，浏览器依赖安装所需）
```

## 10. OWNER_ACTION_REQUIRED（全部后置，未完成）

1. 正式域名 + DNS
2. 腾讯云安全组放行（80/443/8883/8890/8189/TURN；当前未修改）
3. 正式公网 TLS 证书（替换 bootstrap 证书；公网 MQTT 证书单独签发）
4. 公网 SRT 8890 与 WebRTC 8189 暴露方案（含公网 ICE/TURN 验证）
5. off-host 备份（COS/第二云盘）
6. 真实车辆 MQTT credential（per-robot ACL）与 SRT 发布接入（**per-robot/per-stream 发布凭据评估——bootstrap 全局 token 不是最终多车模型**）
7. 真实机器人 downlink 专项授权（当前 DOWNLINK=false）

## 11. 已知剩余风险

- 单机单副本（manual 多副本会重复发布脉冲；FIREBOT_SERVICE_INSTANCE_ID 已预留）
- 监控/告警栈未引入（platform/monitoring 仅目录）
- compose v5 对 override 内相对路径的解析存在偏移（已规避：override 一律 runtime 绝对路径）
- bootstrap admin 密码已在验收中轮换（真值以 runtime/secrets/app/bootstrap_admin_password 为准）
