# Vehicle Agent V1 首次上线与回滚 SOP

本文件是未来取得上线授权后的操作顺序。S5A.1 只修改开发仓库和隔离测试，不执行生产操作。服务器网络、备份和封存证据由 pre-S5B 最终复核确认。

## 发布门槛

必须先通过全部 CI（包括两个真实容器 E2E），取得最终人工合并前审查和上线授权。首次只读灰度设置 `VEHICLE_AGENT_V1_ROLLOUT=true`，保持 `VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false`。启用 v1 车辆、downlink 或显式 rollout 均要求 adapter。不要把仓库 ACL 模板当成服务器运行 ACL 已更新的证明。

1. **备份**：在服务器受限目录保存当前 broker 实际 ACL、密码文件、配置、环境、部署 manifest 和数据库备份，保持原权限；验证备份可恢复。不把密码或 ACL 全文写入验收日志。
2. **校验**：核对批准的 40 位 SHA、clean 工作区、镜像 revision、迁移计划及网络。运行 preflight。缺少 ACL 权限时得到 FAIL 是预期阻断，不能绕过。
3. **更新 ACL**：由授权运维人工比较历史 runtime ACL 与模板，保留既有身份和 canonical 权限，添加下面的最低权限。仓库脚本不会自动修改 ACL。
4. **broker reload/restart**：本部署用 `mosquitto_config` volume 保存实际 `/mosquitto/secure/acl`。仅修改 `secrets/mosquitto/acl` 不会更新这个 volume。由运维按已批准 broker 变更步骤更新 volume，再 reload/restart；保留原文件和恢复步骤。禁止在常规 `all-app` 中隐式重启 broker。
5. **ACL 验证**：`python3 scripts/check_vehicle_agent_acl.py secrets/mosquitto/acl` 检查来源；`SERVER_ENV_FILE=... scripts/server-preflight.sh` 同时检查运行中 broker 的实际文件。`server-verify.sh` 再检查实际 ACL。它们只打印 PASS/FAIL，不打印 ACL/密码。检查器要求明确规则；有 deny 或使用更宽泛替代规则时保守拒绝，由运维审查。
6. **迁移**：备份验证后，按批准迁移计划运行 `BACKUP_VERIFIED=1 TARGET_SHA=<批准SHA> scripts/server-deploy.sh migrate`；记录迁移前版本，供部署 manifest 使用。
7. **一次 all-app 切换**：首次 S5B 顺序固定为 **ACL → migrate → all-app**。禁止 adapter-first：不得先单独部署 adapter 再部署 all-app，以免提前改写 SSOT。rollout=true 时 all-app 已包含 vehicle-agent-adapter；首次命令必须显式带下节的 legacy manifest。保持 downlink=false，构建和重建应用后检查 health、心跳与 revision。
8. **只读上行**：先检查 status 约 5 秒/变化、telemetry 约 1 秒，超过 20 秒无 ONLINE/STALE 抖动；确认车辆身份、boot、地图和数据质量。默认 status stale=8s，offline=15s。telemetry 只证明连接，不替代控制 status；明确 offline 不被旧 telemetry 覆盖。
9. **最后才允许 downlink**：现场独立验证控制、ACK、地图、安全合同后，经单独授权打开 downlink，按停机方案验证。adapter 心跳缺失/过期、status 陈旧均拒绝下发。灰度结束后保存只包含结果和版本的验收记录。

最低 ACL：平台账号 `platform` 对 `_platform/vehicle-agent-v1/+/+`、`v1/robot/+/+` 各有 `topic readwrite`；车辆使用 `pattern read v1/robot/%u/cmd`，对 ack/status/telemetry/availability/event 各有 `pattern write v1/robot/%u/<kind>`。`%u` 必须是设备用户名。若平台账号不同，先使用检查器的 `--platform-user` 显式验证，正式脚本默认账号仍为 platform，不能默默换账号上线。

## 首次 dirty legacy → clean release

旧生产 HEAD provenance 为 `584efcfda60b8d39c516a9939bc0481609fc6f3c`，但 `LEGACY_RUNTIME_EXACT_GIT_SHA=NO`。第一次灰度的 `FIRST_CANARY_ROLLBACK_MODE=LEGACY_IMAGE_SNAPSHOT`；纯 Git checkout 584efcf 无法恢复该 dirty runtime。必须使用 S5A 已封存的真实镜像和配置，不能临时从该 SHA 重建并冒充封存镜像。

运维在受限目录准备 JSON manifest（schema_version=1）。以下为字段形状，所有占位值必须替换为封存证据，不可直接执行：

```json
{
  "schema_version": 1,
  "runtime_kind": "legacy_snapshot",
  "project_name": "firebot-server",
  "source_dir": "/opt/firebot/legacy-s5a/source",
  "compose_file": "/opt/firebot/legacy-s5a/source/docker-compose.server.yml",
  "env_file": "/opt/firebot/legacy-s5a/runtime.env",
  "override_file": "/opt/firebot/legacy-s5a/images.override.yml",
  "override_sha256": "<封存 override 文件的64位小写SHA256>",
  "git_provenance_sha": "584efcfda60b8d39c516a9939bc0481609fc6f3c",
  "services": {
    "api": {"image_ref": "<封存ref>", "expected_image_id": "sha256:<64位ID>"},
    "mqtt-ingress": {"image_ref": "<封存ref>", "expected_image_id": "sha256:<64位ID>"},
    "command-dispatcher": {"image_ref": "<封存ref>", "expected_image_id": "sha256:<64位ID>"},
    "task-worker": {"image_ref": "<封存ref>", "expected_image_id": "sha256:<64位ID>"},
    "web": {"image_ref": "<封存ref>", "expected_image_id": "sha256:<64位ID>"},
    "ros-compat-adapter": {"image_ref": "<封存ref>", "expected_image_id": "sha256:<64位ID>"}
  }
}
```

manifest 本身与所有路径必须为已存在的绝对路径；文件及 source_dir 不可为空。服务集合必须恰好是上述六个。override_sha256 必填，可额外加入 compose_sha256、env_sha256；提供的 hash 均须匹配文件。git_provenance_sha 是来源说明，可选，但旧 SSOT 含 current_sha 时必须提供且匹配。未知字段、重复键、相对路径、缺文件、错误项目名、镜像不存在或 ID 错配都会拒绝；不得写入密码、token、secret 内容、private key 或 ACL 全文。

从已批准新 SHA 的 clean checkout 执行以下形状的命令（仅供后续获批操作）：

```bash
python3 scripts/legacy_runtime.py validate /opt/firebot/legacy-s5a/manifest.json
FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST=/opt/firebot/legacy-s5a/manifest.json \
  TARGET_SHA=<批准40位SHA> scripts/server-deploy.sh all-app
```

FIRST_CANARY_LEGACY_ROLLBACK_MANIFEST 只允许第一次 canary 显式配置，不要永久写入环境文件。old-format SSOT 向后兼容；显式标为 exact_sha 的 clean release 会拒绝再使用首次覆盖。若已恢复为 legacy_snapshot 后重试首次切换，仍须显式指定同一个 manifest。首次只允许 all-app，禁止借此分步改写 SSOT。构建前校验 manifest、文件 hash、本地镜像 ID 和最终 Compose image；失败立即停止。

首次 build/recreate 失败时，统一 snapshot helper 停止并移除本项目新 v1 adapter，然后仅重建六个 legacy 服务，禁止 build/pull 镜像、禁止依赖重建。输出 AUTO_ROLLBACK_MODE=LEGACY_SNAPSHOT 和 AUTO_ROLLBACK_LEGACY=PASS/FAIL；恢复后逐一检查 running、healthy、实际 image ID。不会重建 postgres/redis/mosquitto/nginx/mediamtx，不会执行数据库 downgrade、restore 或 ACL 修改。任何错误都不报告恢复成功，交由人工处置。

首次成功的 SSOT：current_runtime_kind=exact_sha、current_sha=新 SHA；previous_runtime_kind=legacy_snapshot、previous_runtime_manifest=绝对 manifest 路径、previous_git_provenance_sha=旧 provenance。legacy descriptor 不写 previous_sha，避免将 provenance 当成完整版本。

## 按 runtime kind 回滚与验证

先关闭 downlink，保留只读观察；停止现场任务并按安全规程人工确认。运行 `scripts/server-deploy.sh rollback`，脚本从 SSOT 选择 previous runtime：legacy_snapshot 使用 manifest 恢复，绝不 checkout provenance；exact_sha 才调用既有 exact-SHA helper。保留原 S3.1 的纯 584efcf pre-adapter exact-SHA 回滚合同，它与 dirty legacy snapshot 是两种不同路径。

恢复 legacy 后 SSOT 明确记录 current_runtime_kind=legacy_snapshot、current_runtime_manifest 和 current_git_provenance_sha，不写 current_sha。`status` 输出 RUNTIME_KIND=legacy_snapshot；`verify` 验证封存镜像 ID 与 health，不把 provenance revision 当成 exact-SHA。exact_sha 的 verify 继续验证镜像 revision SHA。失败恢复不覆盖 SSOT，必须保留失败证据，禁止将旧 SSOT 当成运行健康证明。

legacy → clean A 成功，随后 clean A → clean B 成功时，previous_runtime_kind 自动变为 exact_sha，previous_sha=A，移除 previous legacy 字段。此后 B → A 使用普通 exact-SHA 回滚，不再依赖首次 snapshot 参数。

发布和显式回滚均在运行态变更前，对 SSOT 目录及本次 manifest 目标目录执行 create/fsync/atomic replace 探测；不改写现有 SSOT 或 manifest。探测失败不构建、不重建。容器切换只是 runtime mutation，只有 SSOT 和原子 manifest 都落盘成功才提交发布成功。

任一终态落盘失败均按强一致策略补偿：输出 FINALIZE_SSOT=FAIL 或 FINALIZE_MANIFEST=FAIL，统一恢复部署前 runtime（legacy/exact 都使用同一选择入口），再修复 current=部署前 runtime、previous=部署前 previous。整体命令仍失败，不得因补偿成功报告发布成功。RUNTIME_COMPENSATION=PASS 只证明运行态已恢复；SSOT_REPAIR=FAIL 或运行态恢复失败时输出 HIGH_PRIORITY_MANUAL_INTERVENTION=YES，保留失败证据并人工核对运行态和 SSOT，不将状态文件当成健康证明。该补偿不执行 DB downgrade/restore 或 ACL 修改。

应用回滚不会自动降级数据库，也不会自动撤销 ACL；这两项必须使用经过审核的兼容和恢复计划。确认无 v1 启用车辆后才可移除 rollout/profile。若 adapter unhealthy 或 ACL 校验失败，发布判定必须 FAIL。

## all-app 稳定后的 ros-compat 维护刷新

首次 all-app 稳定并经批准后，可按需运行 `TARGET_SHA=<current_sha> scripts/server-deploy.sh refresh-ros-compat`。要求 clean checkout、current_runtime_kind=exact_sha 且 current_sha==TARGET_SHA；legacy runtime 或 SHA 不一致均在 build 前拒绝。

该命令仅构建该 SHA 的 Python 镜像、重建 ros-compat-adapter，验证 running/healthy 与 revision；不经 deploy_scope，不写 current.json，不移动 current/previous SHA 或 runtime kind。成功必须输出 ROS_COMPAT_REFRESH=PASS、SSOT_CHANGED=NO，SSOT byte-for-byte 不变。失败不得伪报成功，也不会连带重建其他应用或基础服务。不要用 adapter deploy 替代维护刷新，更不能在首次 all-app 前借维护命令跳过前置要求。

## ACK 诊断

`transport_timeout` 只表示 90 秒窗口未收到车端 ACK，Command 保留 `ACK_MISSING`、空 ack_at/ack_status 和未确认状态。同 vehicle/msg_id/command_id/message_id/boot 的真实晚到 ACK 可覆盖它；首个真实 ACK 不被重复 ACK 或后续计时器覆盖。新鲜 status 可让 Task 反映 observed running/completed，但不会伪造 accepted ACK；`parameters_json.ack_diagnostic` 保留缺失事实。boot 改变后旧 ACK 拒绝。
