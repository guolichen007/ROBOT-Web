# Vehicle-Agent-V1 只读 Agent（G1-R0 修订）安装与回滚方案

> 状态：**方案文档，本轮（G1-R0）未执行任何安装/启动/系统修改**（`PRODUCTION_MUTATION=NONE`）。
> 本方案供 G1 正式安装（PRODUCTION_CUTOVER）取得授权后按序执行，每步保留证据。
> 与《Vehicle-Agent-V1首次上线与回滚SOP》配合：SOP 管服务器侧（ACL/migrate/all-app），
> 本方案管 001 工控机车端侧。
>
> **G1-R0 修订点（R001 现场证据校正）**：
> - 旧服务真实 unit 名：**`firebot-bridge.service`**（非此前草案名称）
> - 旧程序目录：**`/opt/firebot/vehicle-bridge`**
> - 旧配置目录：**`/etc/firebot`**
> - 旧 Bridge 当前状态：**inactive / disabled**（无需现场停用，但仍须二次确认）
> - **不得假定旧 MQTT 凭证仍可使用**：新 Agent 只用新发放的 per-device 凭证

## 1. 目标与边界

| 项 | 值 |
| --- | --- |
| 设备 | 001 工控机，canonical ID `firebot-vehicle-01`，云端显示名 `firebot-001` |
| 新 Agent | `services/vehicle_agent_v1_readonly`（v0.1.1+，只读遥测，无控制能力） |
| 旧资产 | `/opt/firebot/vehicle-bridge`、`firebot-bridge.service`、`/etc/firebot` 配置、旧凭证 —— **保留并隔离，绝不删除/覆盖/复用** |
| 本轮边界 | 不安装、不启动生产服务、不修改工控机系统文件、不使用旧凭证、不触碰 R002 |

## 2. 前置条件（G1 执行前必须全部满足）

1. 本仓库独立分支 `feature/r001-v1-readonly-g1` 全部 CI 通过（含真实组件离线 E2E）。
2. 运维已发放 **per-device** MQTT 凭证（username = `firebot-vehicle-01`，独立随机密码，
   与 R002 / 旧 R001 / 旧 bridge 凭证无关）与生产 broker CA。
3. 服务器侧 ACL 已按 SOP 更新并验证。
4. 工控机已安装 Python 3.11（合同模块 `services/vehicle_agent_v1.py` 使用 `datetime.UTC`）。
5. 现场确认 `firebot-bridge.service` 保持 inactive/disabled（preflight 第 1 项自动复核）。

## 3. 旧资产保留与隔离（先做，全部只读归档）

> 原则：**只复制、只改名、只停用（如需），不删除**。归档物为回滚路径。

```bash
# 3.1 建立归档目录（受限权限 700），记录归档清单
sudo mkdir -p /opt/firebot/legacy-archive/vehicle-bridge-$(date +%Y%m%d%H%M%S)
# 3.2 旧程序与配置原样复制归档（原文件不动）
sudo cp -a /opt/firebot/vehicle-bridge/. \
  /opt/firebot/legacy-archive/vehicle-bridge-*/program/
sudo cp -a /etc/firebot/. \
  /opt/firebot/legacy-archive/vehicle-bridge-*/config/
# 3.3 旧 unit 归档（不删除原 unit 文件）
sudo cp /etc/systemd/system/firebot-bridge.service \
  /opt/firebot/legacy-archive/vehicle-bridge-*/firebot-bridge.service.bak
# 3.4 生成归档 manifest（文件清单 + SHA256），留审计证据
(cd /opt/firebot/legacy-archive/vehicle-bridge-* && sha256sum $(find . -type f | sort)) \
  | tee /opt/firebot/legacy-archive/vehicle-bridge-*/SHA256SUMS
```

归档后**验收**：原目录/文件未被修改（对比归档前后 SHA256）；归档目录权限 700；
**旧凭证仅归档、绝不复用**（新 Agent 与旧 bridge.env 完全隔离，Agent 结构上拒绝读取
`bridge.env` 文件名）；密码/凭证内容不进任何日志与验收记录。

## 4. 新 Agent 安装步骤（G1 获批后执行，本轮不执行）

```bash
# 4.0 只读 preflight 第一阶段（fail-closed，任何 FAIL 即停止，禁止继续安装）
sudo bash scripts/preflight_vehicle_agent_v1_readonly.sh preinstall
# PREINSTALL=环境预检：旧桥 unit 状态/进程、磁盘空间、Python3.11、身份冲突、
# 网络与 CA 预检（交付物缺失只 WARN，真实冲突才 FAIL）

# 4.1 部署目录：/opt/firebot/vehicle-agent-v1-readonly（独立于旧目录）
sudo mkdir -p /opt/firebot/vehicle-agent-v1-readonly
sudo cp -a <批准SHA的仓库检出> /opt/firebot/vehicle-agent-v1-readonly/repo
# 4.2 虚拟环境与依赖锁定（可复现）
sudo /usr/bin/python3.11 -m venv /opt/firebot/vehicle-agent-v1-readonly/venv
sudo /opt/firebot/vehicle-agent-v1-readonly/venv/bin/pip install \
  -r /opt/firebot/vehicle-agent-v1-readonly/repo/services/vehicle_agent_v1_readonly/requirements.txt

# 4.3 先建服务用户/组，再执行需要该身份的权限设置（顺序不可颠倒）
sudo useradd --system --no-create-home --shell /usr/sbin/nologin firebot

# 4.4 受控凭证注入：/etc/firebot/vehicle-agent-v1-readonly/（与旧 /etc/firebot 分开）
sudo mkdir -p /etc/firebot/vehicle-agent-v1-readonly
sudo install -m 640 device_identity.firebot-vehicle-01.json \
  /etc/firebot/vehicle-agent-v1-readonly/device_identity.json   # 生产密码只在此文件
sudo chown root:firebot /etc/firebot/vehicle-agent-v1-readonly/device_identity.json
sudo install -m 644 <运维发放的生产CA> \
  /etc/firebot/vehicle-agent-v1-readonly/ca.crt
# 权限方案（G1-R1 修正）：root:firebot + 0640 —— 服务用户组内可读（非 root 可读取）、
# Agent 自身不可写、其他普通用户不可读。Agent 启动时强校验"禁止组写 + 禁止其他任意位"
# （0640/0600 允许，0644/0660 拒绝）。systemd LoadCredential（≥247）仅在预检确认
# 目标机 systemd 版本后作为可选替代，不照搬新功能。

# 4.5 运行目录
sudo mkdir -p /var/lib/firebot/vehicle-agent-v1-readonly
sudo chown firebot:firebot /var/lib/firebot/vehicle-agent-v1-readonly
sudo chmod 700 /var/lib/firebot/vehicle-agent-v1-readonly

# 4.6 systemd unit（模板：services/vehicle_agent_v1_readonly/deploy/...service）
sudo install -m 644 \
  services/vehicle_agent_v1_readonly/deploy/firebot-vehicle-agent-v1-readonly.service \
  /etc/systemd/system/firebot-vehicle-agent-v1-readonly.service
sudo systemctl daemon-reload

# 4.7 只读 preflight 第二阶段（产物与运行态预检，全部通过才允许启动）
sudo bash scripts/preflight_vehicle_agent_v1_readonly.sh prestart
# PRESTART=服务用户/组、venv+依赖锁定、凭证权限+服务用户实际可读性、运行目录、
# unit 语法（systemd-analyze verify）、单 owner 实际 flock 获取验证、旧桥复查

systemctl status firebot-bridge.service   # 现场证据复核：必须仍为 inactive
sudo systemctl start firebot-vehicle-agent-v1-readonly
```

凭证铁律：生产密码只允许出现在 `/etc/firebot/vehicle-agent-v1-readonly/device_identity.json`
（0640，root:firebot）；禁止写进普通配置、源码、仓库、systemd unit、命令行参数或环境文件。
Agent 对身份文件做权限强校验（组写/其他任意位 → 拒绝启动）。

### 4.8 隔离 systemd 验收（G1-R1 已执行证据）

`scripts/systemd_readonly_acceptance.sh` 在隔离环境（systemd 245、真实低权限 UID/GID、
systemd-run 瞬态单元、不写 /etc/systemd/system）**14/14 通过**：非 root 服务用户可读 0640
凭证、nobody 不可读、Agent 不可写；WorkingDirectory（主进程 cwd 校验）与 PYTHONPATH/venv/
依赖锁定生效；首次启动真实 broker 收到 status；第二实例退出码 3；stop→start 重启语义；
Restart=on-failure（冲突退出 3 自动重启）；SIGTERM 正常退出码 0。验收不连接生产 Broker、
不触碰生产 unit。

单 owner 三重防护（运行期）：systemd 单实例 → Agent 内 flock（run-dir/agent.lock）
→ 旧 `vehicle-bridge` 进程存在即拒绝启动。

## 5. 安装后验证（只读验收）

1. `systemctl status` → active；日志出现 `availability=online published`（**含
   broker PUBACK 确认**，未确认时会打印 UNCONFIRMED 而不是宣称成功）。
2. 云端 Robot `firebot-vehicle-01` ONLINE；status 每 3s 一条、新鲜（云端 stale 门限 8s）；
   `programs.agent=true`，其余程序键 false（未接入，不伪造）。
3. 确认该车辆无任何下行命令订阅；`VEHICLE_AGENT_V1_DOWNLINK_ENABLED=false` 保持。
4. 检查 `host-metrics.jsonl` 正常滚动写入（本地可观测，默认不上行）。
5. 验收记录只保存结论（PASS/FAIL）与时间，不保存凭证、密码、ACL 全文。

## 6. 回滚方案（任一步失败或现场异常时）

1. 停止新 Agent：`systemctl stop firebot-vehicle-agent-v1-readonly`。
   Agent 退出前发布 retained `availability=offline` 并等待 PUBACK（15s 超时上限），
   云端显示 OFFLINE。
2. 旧 Bridge 恢复路径：`firebot-bridge.service` 与 `/opt/firebot/vehicle-bridge`、
   `/etc/firebot` 均未被修改，按需 `systemctl start firebot-bridge`（如需旧链路）。
   若旧凭证失效则不得伪造——交由人工处置，不自动恢复旧 MQTT。
3. 若需完全移除新 Agent：删除 `/opt/firebot/vehicle-agent-v1-readonly`、
   `/etc/firebot/vehicle-agent-v1-readonly`、`/var/lib/firebot/vehicle-agent-v1-readonly`
   与新 unit；旧目录/旧 unit/归档目录保留。
4. 回滚后验证云端状态与旧链路；全程不触碰 R002；归档目录保留至 G1 验收通过。

## 7. 安全边界再确认（安装执行者必读）

- 新 Agent 与旧 Bridge **互斥**（owner 守卫：检测到 `vehicle-bridge` 进程即拒绝启动）。
- 新 Agent 不订阅任何 topic、不发布 `/cmd_vel`、不写 Modbus/CAN/GPIO/继电器。
- 凭证 per-device、受控文件注入、权限受检；旧 `bridge.env` 与新 Agent 完全隔离。
- 本方案不含任何"改旧文件"的步骤；旧资产在任何步骤中都是只读对象。
