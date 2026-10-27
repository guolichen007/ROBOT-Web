# U-Dao 云服务器部署

> U-Dao（腾讯云 CVM 上海，Ubuntu 22.04.5，8C/15G，系统盘 vda 100GiB + 数据盘 vdb 200GiB）的正式云部署说明。
> 本文只描述 U-Dao 云部署；旧现场服务器方案见《服务器部署.md》（Tailscale/现场局域网内容标注为**历史现场方案**，不适用于云）。
> 服务器本地运维文档（目录/端口/权限/恢复）见 `/srv/u-dao/docs/`（不提交本仓库）。

## 1. 部署模型（不可变 release + fresh 状态机）

- **GitHub code ≠ running runtime**：只接受 exact 40 位 SHA 部署，禁止「拉最新」。
- 开发支线：`feature/u-dao-cloud-deployment-v1`（从唯一产品基线 `60d30bbe…` 创建，禁止从 main 开发、禁止 merge main）。
- 目录：
  - 开发 repo：`/srv/u-dao/projects/firebot/repo`（开发/编排用，分支工作区）
  - immutable release：`/srv/u-dao/projects/firebot/releases/<40SHA>`（`git worktree add --detach`，**一经创建禁止 checkout 修改**；rollback 切换 release 目录而非 checkout）
  - runtime 真值：`/srv/u-dao/projects/firebot/runtime/{env,secrets,certs,deployments}`（不入 Git；600/700）
- 版本 SSOT：`runtime/deployments/current.json`（`current/previous` descriptor：`exact_sha | legacy_snapshot | none`）。
- **fresh install 语义**：新服务器 `bootstrap` → `current=exact_sha, previous=none`；首次同 SHA 的 `all-app` 保持 `previous=none`；第二次 exact-SHA 发布轮转 `previous=旧 current` 形成回滚链。禁止伪造 `legacy_snapshot`/`previous SHA`/`BACKUP_VERIFIED`。
- 运行证明三重：`current.json` + 容器 OCI revision label（`org.opencontainers.image.revision`）+ 第三方镜像 RepoDigest（manifest 记录；exact Git SHA 不单独宣称为完整 runtime reproducibility）。

## 2. 权限模型

- 所有 Git/文件操作以 `ubuntu` 执行；`sudo` 只作用于 docker 命令本身（`sudo -n docker …`），**绝不整脚本 sudo**。
- 不加 docker group。部署脚本统一经 `scripts/docker_cmd.sh` 自动检测（`docker` → `sudo -n docker` → fail-closed）。
- 关键坑：`sudo` 的 env_reset 会剥离 shell 导出的变量——
  - compose 插值变量一律以 `--env-file` 为唯一来源（runtime `.env.server` 内含 `SERVER_ENV_FILE=<绝对路径>`）；
  - 镜像 tag 变量（`FIREBOT_PYTHON_IMAGE` 等）经 `sudo -n env K=V docker compose …` 显式传递（`compose_env_run`）。
- secret 分发：compose `file:` 型 secret 是绑定挂载，**保留宿主文件权限**（600/ubuntu → 容器内非 root 用户不可读）。因此 U-Dao 用 `app-secrets-init`（busybox）把 runtime secrets 拷入具名卷 `app_secrets` 并按消费者分发（app 文件 600:10001、`mqtt_platform_password`/`postgres_password`/`redis_password` 444），各服务挂载 `app_secrets:/run/secrets:ro`。**宿主 secret 文件始终保持 600/ubuntu。**
- 权限边界表：repo/release/runtime/platform/backups 均 `ubuntu:ubuntu`；`platform/docker`、`platform/containerd` 归 root（daemon 数据）；secrets 目录 700、文件 600/640。

## 3. 服务器底座（一次性）

见 `deploy/u-dao/scripts/`（先 commit 并记录 SHA256 再执行）：

```bash
sudo bash deploy/u-dao/scripts/00-prepare-data-disk.sh   # vdb→GPT/ext4(label=u-dao,reserved 1%)→挂载 /srv/u-dao
sudo bash deploy/u-dao/scripts/01-install-docker.sh      # 官方源；data-root/containerd root 落数据盘；10.239/10.240 网段；挂载守卫
bash deploy/u-dao/scripts/02-gen-bootstrap-secrets.sh    # runtime secrets + bootstrap CA/证书（BOOTSTRAP_LOCAL_ONLY）
bash deploy/u-dao/scripts/03-render-runtime-config.sh    # mediamtx runtime yml（SRT passphrase）+ ingress fragment 实例化
```

要点：

- **数据盘预检**（脚本内置 fail-closed）：vdb≈200GiB、无分区、无挂载、无 holders、无文件系统签名、`/` 在 vda2；任何不符 `DATA_DISK_PRECHECK=FAIL` 立即停止。
- **Docker**：`data-root=/srv/u-dao/platform/docker`；Docker 29 全新安装默认启用 containerd image store → containerd root 迁 `/srv/u-dao/platform/containerd`；`docker0=10.239.0.1/24`、compose pools `10.240.0.0/16 size=/24`（与腾讯云 VPC 172.17.0.0/20 无重叠，安装脚本先复核）；默认 `local` 日志驱动 + rotation；**major-version gate**（docker 29 / compose 5 为已审计基线，脚本内置）。
- **挂载守卫（fail-closed）**：docker/containerd 的 systemd drop-in `RequiresMountsFor=/srv/u-dao` + `ConditionPathIsMountPoint=/srv/u-dao`——数据盘未挂载时服务拒绝启动（已实测：journal `Condition check resulted in … being skipped`），绝不静默写系统盘。
- **镜像源**：Docker Hub 在中国大陆网络不可直连（实测 i/o timeout）→ `registry-mirrors: mirror.ccs.tencentyun.com`（腾讯云官方）；配置代理后可移除。
- **bootstrap TLS（BOOTSTRAP_LOCAL_ONLY）**：本地 CA + MQTT server cert（SAN ≥ `DNS:mosquitto,DNS:localhost,DNS:u-dao,IP:127.0.0.1`）+ nginx cert（`firebot.local` 等），标记非公网证书；真实车辆接入/正式域名前必须替换（OWNER_ACTION_REQUIRED）。公网 MQTT 域名将来单独换证，不复用 bootstrap cert。

## 4. 部署流程（fresh）

```bash
# 1) 创建 immutable release（当前 HEAD 必须为已 commit 的 exact SHA）
SHA=$(git -C /srv/u-dao/projects/firebot/repo rev-parse HEAD)
git -C /srv/u-dao/projects/firebot/repo worktree add --detach /srv/u-dao/projects/firebot/releases/$SHA $SHA

# 2) 渲染 runtime 配置（从 release 树）
bash /srv/u-dao/projects/firebot/releases/$SHA/deploy/u-dao/scripts/03-render-runtime-config.sh

# 3) 平台 shared ingress（platform 资源，独立于 Firebot 生命周期；见 /srv/u-dao/docs/）
sudo -n docker compose -p u-dao-ingress -f /srv/u-dao/platform/ingress/docker-compose.yml up -d

# 4) fresh bootstrap（基础服务 + fresh DB + SSOT previous=none）
cd /srv/u-dao/projects/firebot/repo
export U_DAO_MODE=1
export FIREBOT_DEPLOY_ROOT=/srv/u-dao/projects/firebot/runtime/deployments
export FIREBOT_RELEASE_ROOT=/srv/u-dao/projects/firebot/releases/$SHA
export FIREBOT_RUNTIME_ROOT=/srv/u-dao/projects/firebot/runtime
export SERVER_ENV_FILE=/srv/u-dao/projects/firebot/runtime/env/.env.server
export FIREBOT_COMPOSE_FILES="$FIREBOT_RELEASE_ROOT/docker-compose.server.yml $FIREBOT_RELEASE_ROOT/deploy/u-dao/compose.u-dao.yml"
TARGET_SHA=$SHA bash scripts/server-deploy.sh bootstrap

# 5) 首发布（同 SHA；保持 previous=none）
TARGET_SHA=$SHA bash scripts/server-deploy.sh all-app

# 6) 验收
TARGET_SHA=$SHA bash scripts/server-deploy.sh verify
bash scripts/cloud_fault_injection.sh
```

- `bootstrap` 仅限 fresh：`current.json` 不存在且 DB 无 `alembic_version`；禁止 `FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST`；已有库的升级必须走 `backup`+`backup-verify`+`migrate`（`BACKUP_VERIFIED=1` 仅在 restore 验证通过后人工确认）。
- 每次发布前：`preflight`（含 v1 ACL gate、NTP 同步、端口语义、legacy/U-Dao 断言）。

## 5. 更新与回滚

- 更新 = 新 commit → 新 release worktree → `bootstrap` 已跳过（直接 `all-app`/分组部署）→ `verify`。**部署前必须 clean commit + exact SHA；验收后改动代码必须重新 commit/release/部署/验证**（`RUNNING_SHA == FINAL_GIT_HEAD`）。
- 回滚 = `rollback` 子命令：从 `releases/<PREVIOUS_SHA>` 切换恢复（**绝不在 immutable release 内 checkout**）；`previous=none` 拒绝回滚；**schema gate**：回滚目标 migration head ≠ 当前 DB revision → 禁止回滚（不伪造 PASS）；U-Dao 守卫：回滚目标必须含 `deploy/u-dao/compose.u-dao.yml`。
- DB 永不 downgrade。备份：`backup`（pg_dump+gzip+sha256+manifest）→ `backup-verify`（隔离临时库 `firebot_backup_verify` 的 restore + 一致性检查；**仅 dump 存在不算 VERIFIED**）。同盘备份 ≠ 灾难恢复（COS/off-host 备份为 OWNER_ACTION）。
- Firebot 的部署/回滚脚本**禁止操作 `u-dao-ingress` project**（平台资源）。

## 6. 已修复的实战坑（记录在案）

1. `lsblk HOLDERS` 列需 util-linux≥2.40（Ubuntu 22.04 为 2.37）→ 数据盘预检用 sysfs `/sys/block/vdb/holders`。
2. `mosquitto_passwd -c` 拒绝覆盖已存在文件 → 先删再生成；容器内 root 产物需 chown 回 ubuntu。
3. compose `ports` 按四元组合并（override 改绑定 IP 会双发布）→ U-Dao override 用 `!override` 整属性替换；基座 `${…:?}` 因按文件解析先于 override 合并而必须软化，legacy fail-closed 由 preflight 恢复（缺变量/占位 IP → FAIL）。
4. compose v5 不接受 secrets 长语法 `mode`；file 型 secret 绑定挂载保留宿主 600 → `app-secrets-init` 卷分发模式。
5. nginx `upstream resolve` 启动期解析失败会 crash → 片段改**变量式 proxy_pass + Docker DNS 运行时解析**（edge 未就绪返回 502，重建后按 TTL 自动恢复，无人工 reload）。
6. `sudo env_reset` 剥离环境变量 → 见第 2 节权限模型。

## 7. 安全边界（云）

- 宿主端口全部 127.0.0.1（见《U-Dao网络与端口规划.md》）；Firebot 不占 80/443；公网 80/443/8883/8890/8189/TURN/DNS/正式 TLS 全部 OWNER_ACTION_REQUIRED。
- `VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false` 全程；真实机器人 downlink 禁止。
- 公网不开放 5432/6379/1883/8554/9997/9998/metrics/ready。
- COOKIE_SECURE=true：完整登录/HMI 必须走 bootstrap HTTPS（18443）；HTTP 18000 仅 health/redirect。
