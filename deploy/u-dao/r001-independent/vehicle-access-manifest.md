# 车辆接入 Manifest（001 实际使用；002/003+ 复用模板）

> 只包含非敏感标识与指纹；密码、CA 私钥、生产环境文件严禁进入本文件与 Git。
> 每辆车一份，放置于部署包车辆目录；发布时随版本记录 SHA。

## 字段契约

| 字段 | 含义 | 001 现值 |
| --- | --- | --- |
| vehicle_id | 业务主键（canonical ID） | `firebot-vehicle-01` |
| display_name | 云端显示名 | `firebot-001` |
| transport_profile | 传输配置（broker/CA/端口） | `r001-independent-v1` |
| mqtt_host | 接入主机名（TLS 主机名验证 SAN 必须覆盖） | `firebot.youdaozd.com` |
| mqtt_port | 独立入口端口 | `8884` |
| ca_fingerprint | 接入 CA SHA256 指纹（交付时以实际签发值为准） | `<签发后回填>` |
| release_sha | 车端 Agent/云端配套版本 | `<冻结 SHA>` |
| execution_profile | 运行/执行能力档 | `telemetry-only`（G1 阶段） |
| control_profile | 控制能力档（G2 起用） | `none`（G1 阶段） |

## 四种标准能力档（execution_profile / control_profile）

| 档位 | 能力 | 生效条件 |
| --- | --- | --- |
| `telemetry-only` | availability/status/telemetry 只读上行；零命令、零订阅 | G1 默认（001 当前） |
| `patrol` | telemetry-only + 任务型命令（patrol/stop_patrol/resume_patrol/homing/navigate/stop_navigate） | 合同验证 + allowlist + OWNER 批准 |
| `manual-control` | patrol + manual/stop-motion/estop/reset-estop | 额外安全评审 + 急停链验收 |
| `full` | manual-control + 全部能力 | 独立评审（仅真实整车验收后） |

## 002/003+ 复用方法

1. 复制本文件 → 替换 vehicle_id/display_name/mqtt_host/mqtt_port/ca_fingerprint/release_sha；
2. transport_profile 沿用 `r001-independent-v1`（新 CA/新账号逐车独立签发）；
3. execution_profile 默认 `telemetry-only`，逐档升级需 OWNER 批准；
4. 车辆账号/ACL 逐车隔离（%u 模式），跨车 topic 零读写。
