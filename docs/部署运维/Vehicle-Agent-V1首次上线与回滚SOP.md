# Vehicle Agent V1 首次上线与回滚 SOP

本文件是未来批准上线后的操作顺序。本轮 S3 只修改仓库和测试环境，不执行生产操作。生产 github.com 网络故障继续 OPEN，恢复并核验前不得进行 exact SHA 发布。

## 发布门槛

必须先通过全部 CI（包括两个真实容器 E2E），取得最终人工合并前审查和上线授权。首次只读灰度设置 `VEHICLE_AGENT_V1_ROLLOUT=true`，保持 `VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false`。启用 v1 车辆、downlink 或显式 rollout 均要求 adapter。不要把仓库 ACL 模板当成服务器运行 ACL 已更新的证明。

1. **备份**：在服务器受限目录保存当前 broker 实际 ACL、密码文件、配置、环境、部署 manifest 和数据库备份，保持原权限；验证备份可恢复。不把密码或 ACL 全文写入验收日志。
2. **校验**：核对批准的 40 位 SHA、clean 工作区、镜像 revision、迁移计划及网络。运行 preflight。缺少 ACL 权限时得到 FAIL 是预期阻断，不能绕过。
3. **更新 ACL**：由授权运维人工比较历史 runtime ACL 与模板，保留既有身份和 canonical 权限，添加下面的最低权限。仓库脚本不会自动修改 ACL。
4. **broker reload/restart**：本部署用 `mosquitto_config` volume 保存实际 `/mosquitto/secure/acl`。仅修改 `secrets/mosquitto/acl` 不会更新这个 volume。由运维按已批准 broker 变更步骤更新 volume，再 reload/restart；保留原文件和恢复步骤。禁止在常规 `all-app` 中隐式重启 broker。
5. **ACL 验证**：`python3 scripts/check_vehicle_agent_acl.py secrets/mosquitto/acl` 检查来源；`SERVER_ENV_FILE=... scripts/server-preflight.sh` 同时检查运行中 broker 的实际文件。`server-verify.sh` 再检查实际 ACL。它们只打印 PASS/FAIL，不打印 ACL/密码。检查器要求明确规则；有 deny 或使用更宽泛替代规则时保守拒绝，由运维审查。
6. **adapter 启动**：设置 rollout 后，以 `TARGET_SHA=<批准SHA> scripts/server-deploy.sh adapter` 或 `control-plane/all-app` 构建并重建 adapter。它使用相同不可变 Python 镜像；manifest 记录 adapter image revision，verify 校验 health、心跳和 SHA。`all-app`、自动回滚和人工 rollback 覆盖 adapter。
7. **只读上行**：先检查 status 约 5 秒/变化、telemetry 约 1 秒，超过 20 秒无 ONLINE/STALE 抖动；确认车辆身份、boot、地图和数据质量。默认 status stale=8s，offline=15s。telemetry 只证明连接，不替代控制 status；明确 offline 不被旧 telemetry 覆盖。
8. **最后才允许 downlink**：现场独立验证控制、ACK、地图、安全合同后，经单独授权打开 downlink，按停机方案验证。adapter 心跳缺失/过期、status 陈旧均拒绝下发。灰度结束后保存只包含结果和版本的验收记录。

最低 ACL：平台账号 `platform` 对 `_platform/vehicle-agent-v1/+/+`、`v1/robot/+/+` 各有 `topic readwrite`；车辆使用 `pattern read v1/robot/%u/cmd`，对 ack/status/telemetry/availability/event 各有 `pattern write v1/robot/%u/<kind>`。`%u` 必须是设备用户名。若平台账号不同，先使用检查器的 `--platform-user` 显式验证，正式脚本默认账号仍为 platform，不能默默换账号上线。

## 回滚

先关闭 downlink，保留只读观察；停止现场任务并按安全规程人工确认。按 deployment `previous_sha` 回滚应用及 adapter，验证所有 image revision 和 adapter 心跳。应用回滚不会自动降级数据库，也不会自动撤销 ACL；这两项必须使用经过审核的兼容和恢复计划。确认无 v1 启用车辆后才可移除 rollout/profile。若 adapter unhealthy 或 ACL 校验失败，发布判定必须 FAIL。

## ACK 诊断

`transport_timeout` 只表示 90 秒窗口未收到车端 ACK，Command 保留 `ACK_MISSING`、空 ack_at/ack_status 和未确认状态。同 vehicle/msg_id/command_id/message_id/boot 的真实晚到 ACK 可覆盖它；首个真实 ACK 不被重复 ACK 或后续计时器覆盖。新鲜 status 可让 Task 反映 observed running/completed，但不会伪造 accepted ACK；`parameters_json.ack_diagnostic` 保留缺失事实。boot 改变后旧 ACK 拒绝。
