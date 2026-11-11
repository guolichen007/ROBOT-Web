# Vehicle-Agent-V1 只读 Agent（G0-A2）安装与回滚方案

> 状态：**方案文档，本轮（G0-A2）未执行任何安装/启动/系统修改**（`DEPLOYMENT_EXECUTED=NO`）。
> 本方案供 G1（正式安装准备）取得授权后按序执行，每步保留证据。
> 与《Vehicle-Agent-V1首次上线与回滚SOP》配合使用：SOP 管服务器侧（ACL/migrate/all-app），
> 本方案管 001 工控机车端侧。

## 1. 目标与边界

| 项 | 值 |
| --- | --- |
| 设备 | 001 工控机，canonical ID `firebot-vehicle-01`，云端显示名 `firebot-001` |
| 新 Agent | `services/vehicle_agent_v1_readonly`（v0.1.0，只读遥测，无控制能力） |
| 旧资产 | `/opt/firebot/vehicle-bridge`、旧 systemd unit、旧配置、旧凭证 —— **保留并隔离，绝不删除/覆盖** |
| 本轮边界 | 不安装、不启动生产服务、不修改工控机系统文件、不使用旧凭证、不触碰 R002 |

## 2. 前置条件（G1 执行前必须全部满足）

1. 本仓库独立分支 `feature/r001-v1-readonly-g1` 的全部 CI 通过（G0-A2 交付物）。
2. 运维已发放 **per-device** MQTT 凭证（username = `firebot-vehicle-01`，独立密码，与
   R002 / 旧 R001 测试凭证无关）与生产 broker CA（见《云端接入最小操作计划》）。
3. 服务器侧 ACL 已按 SOP 更新并验证（车辆 `pattern read v1/robot/%u/cmd`、
   `pattern write v1/robot/%u/{ack,status,telemetry,availability,event}`）。
4. 工控机已安装 Python ≥3.11（合同模块 `services/vehicle_agent_v1.py` 使用 `datetime.UTC`）。
5. 旧 bridge 处于可观察状态：本次切换前先记录旧进程状态，切换后按第 5 节验证。

## 3. 旧资产保留与隔离（先做，全部只读归档）

> 原则：**只复制、只改名、只停用，不删除**。归档物为回滚路径。

```bash
# 3.1 建立归档目录（受限权限 700），记录归档清单
sudo mkdir -p /opt/firebot/legacy-archive/vehicle-bridge-$(date +%Y%m%d%H%M%S)
# 3.2 旧程序与配置原样复制归档（保留原文件不动，避免影响运行中进程）
sudo cp -a /opt/firebot/vehicle-bridge/. \
  /opt/firebot/legacy-archive/vehicle-bridge-*/program/
sudo cp -a /opt/firebot/vehicle-bridge/bridge.env \
  /opt/firebot/legacy-archive/vehicle-bridge-*/bridge.env   # 旧凭证归档，绝不复用
# 3.3 旧 systemd unit 归档（不删除原 unit 文件）
sudo cp /etc/systemd/system/firebot-vehicle-bridge.service \
  /opt/firebot/legacy-archive/vehicle-bridge-*/firebot-vehicle-bridge.service.bak
# 3.4 生成归档 manifest（文件清单 + SHA256），留审计证据
(cd /opt/firebot/legacy-archive/vehicle-bridge-* && sha256sum $(find . -type f | sort)) \
  | tee /opt/firebot/legacy-archive/vehicle-bridge-*/SHA256SUMS
```

归档后**验收**：原目录文件未被修改（对比归档前后 SHA256）；归档目录权限 700；
密码/凭证内容不进任何日志与验收记录。

## 4. 新 Agent 安装步骤（G1 获批后执行）

```bash
# 4.1 部署目录：/opt/firebot/vehicle-agent-v1-readonly（独立于旧目录，不覆盖任何旧文件）
sudo mkdir -p /opt/firebot/vehicle-agent-v1-readonly
sudo cp -a <批准SHA的仓库检出>/services/vehicle_agent_v1_readonly \
  /opt/firebot/vehicle-agent-v1-readonly/service/
sudo cp <批准SHA的仓库检出>/services/vehicle_agent_v1.py \
  /opt/firebot/vehicle-agent-v1-readonly/service/../   # 合同模块单一来源，保持相对导入路径
# 4.2 设备身份配置（G1 正式凭证，per-device，禁止占位标记，禁止 --allow-placeholder-credentials）
sudo install -m 600 device_identity.firebot-vehicle-01.json \
  /opt/firebot/vehicle-agent-v1-readonly/device_identity.json
sudo install -m 644 <运维发放的生产CA> \
  /opt/firebot/vehicle-agent-v1-readonly/ca.crt
# 4.3 systemd unit（新 unit 名，与旧 unit 并存、互不覆盖）
sudo install -m 644 firebot-vehicle-agent-v1-readonly.service \
  /etc/systemd/system/firebot-vehicle-agent-v1-readonly.service
sudo systemctl daemon-reload
# 4.4 启动前守护验证：旧 bridge 已停止（Agent 自带 owner 守卫会双重确认）
systemctl stop firebot-vehicle-bridge   # 或按现场顺序人工确认进程退出
systemctl start firebot-vehicle-agent-v1-readonly
```

systemd unit 建议形状（安装时生成，不提交生产凭证）：

```ini
[Unit]
Description=Firebot Vehicle Agent V1 (readonly telemetry)
After=network-online.target

[Service]
Type=simple
User=firebot
ExecStart=/usr/bin/python3 -m services.vehicle_agent_v1_readonly.main \
  --config /opt/firebot/vehicle-agent-v1-readonly/device_identity.json \
  --run-dir /var/lib/firebot/vehicle-agent-v1-readonly
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

## 5. 安装后验证（只读验收）

1. `systemctl status` → active；日志出现 `availability=online published`。
2. 云端 Robot `firebot-vehicle-01` 显示 ONLINE；status 新鲜（<10s）；
   `programs.agent=true`，其余程序键为 false（未接入，不伪造）。
3. 平台侧确认该车辆**没有任何下行命令订阅**（Agent 结构上不订阅，ACL 亦只读自己的 cmd）。
4. 检查 `host-metrics.jsonl` 正常滚动写入（本地可观测，默认不上行）。
5. 验收记录只保存结论（PASS/FAIL）与时间，不保存凭证、密码、ACL 全文。

## 6. 回滚方案（任一步失败或现场异常时）

1. 停止新 Agent：`systemctl stop firebot-vehicle-agent-v1-readonly`。
   Agent 退出前发布 retained `availability=offline`（正常退出语义），云端显示 OFFLINE。
2. 恢复旧 bridge：`systemctl start firebot-vehicle-bridge`（unit 未被删除，配置/凭证未动）。
3. 若需完全移除新 Agent：删除新目录与新 unit（旧目录/旧 unit/归档目录保留）。
4. 回滚后验证旧链路：旧 bridge 恢复上报、云端状态恢复旧语义。
5. 全程不触碰 R002；归档目录保留至少到 G1 验收通过。

## 7. 安全边界再确认（安装执行者必读）

- 新 Agent 与旧 bridge **互斥**（owner 守卫：检测到 `vehicle-bridge` 进程即拒绝启动）。
- 新 Agent 不订阅任何 topic、不发布 `/cmd_vel`、不写 Modbus/CAN/GPIO/继电器。
- 凭证 per-device；旧 `bridge.env` 与新 Agent 完全隔离（新 Agent 拒绝读取该文件名）。
- 本方案不含任何"改旧文件"的步骤；旧资产在任何步骤中都是只读对象。
