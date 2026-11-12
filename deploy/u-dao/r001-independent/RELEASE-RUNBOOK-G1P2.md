# R001 G1-P2 生产执行包（发布运行手册）

> 状态：待 OWNER 授权。本包不包含任何真实凭证/密钥值；执行环境按本文件逐项核对。
> 基线：FINAL_SHA 见随版本 manifest；镜像以部署记录 image_id+revision 为准。

## 0. 授权与边界（全部为 OWNER 项，未授权禁止执行）

1. 生产 Firebot DB 备份 + 隔离恢复演练通过。
2. 腾讯云安全组放行独立入口（默认 8884/tcp，公网）。
3. R001 正式注册（preregister enabled=false + 协议绑定 VEHICLE_AGENT_V1）。
4. 云端 API/Web 升级至含 G1-P2-L0 的批准 SHA（无迁移；回滚=旧镜像标签）。
5. 物理 Agent 安装（vehicle_agent_v1_readonly，正式凭证）。

## 1. 执行前保护检查（防止误重建生产服务）

```bash
# 1) 确认使用本目录模板与独立 env-file，绝不 -f 指向生产 compose 文件
ls deploy/u-dao/r001-independent/compose.r001-independent.yml
test -f /path/to/r001.env || exit 1
# 2) 项目名隔离（默认 name: u-dao-firebot-r001），绝不与 u-dao-firebot 混用
docker compose -f deploy/u-dao/r001-independent/compose.r001-independent.yml \
  --env-file /path/to/r001.env config --quiet || exit 1
# 3) 端口核对（G1-P2-L0 A4 修正）：只核验本次新增的 8884 绑定未被其它进程占用；
#    生产端口（18080/18554/18883 等）为生产正常占用，仅用于前后不变性对比，
#    不得判为冲突。
if ss -ltn | grep -qE ':(8884)\b'; then echo '8884 已被占用'; exit 1; fi
ss -ltn | grep -E ':(18080|18554|18883)\b' > /root/g1p2-prod-ports-before.txt || true
# 4) 生产服务身份快照（容器 ID/StartedAt）留存，验收时逐项比对
docker ps --format '{{.Names}} {{.ID}} {{.Status}}' | grep '^u-dao-firebot' > /root/g1p2-prod-snapshot.txt
```

## 2. 执行顺序（每步 STOP 条件与回滚）

| 步骤 | 执行者 | 变更资源 | STOP 条件 | 回滚 |
| --- | --- | --- | --- | --- |
| 1 OWNER 授权 | OWNER | 无 | 未获 GO 即停 | - |
| 2 生产 Firebot DB 备份+隔离恢复 | 云端 CC | 新增备份文件 | 恢复比对失败 | 备份仅追加 |
| 3 生产镜像/环境备份 + 最小 API/Web 升级 | 云端 CC | api/web 镜像标签 + env 备份 | 健康检查失败 | 旧镜像标签 |
| 4 独立 Broker、新 CA、账号及 TLS 准备 | 云端 CC | 新证书/账号（隔离目录） | 任何生成/校验失败 | 删除新件 |
| 5 受控创建 firebot-vehicle-01 并绑定 V1 | 云端 CC | robots 新行(enabled=false)+audit | 重复/绑定闸拒绝 | 行保持 disabled |
| 6 腾讯云 8884 访问控制 + 实际外部握手验证 | OWNER/云端 CC | 安全组规则 + 8884 公网 | 握手失败不继续 | 移除规则 |
| 7 001 低权限安装只读 Agent | 车端 CC | 001 工控机 systemd | owner 冲突(退出码3) | 停 unit |
| 8 实际 Web 上行与 R002 隔离验收 | 双方 CC | 无 | R002/生产任何异常 | 停 Agent+`down` |

> 禁止运行旧 Fleet 脚本去同步或 HUP 原生产 Broker（生产 broker 零 SIGHUP/零 ACL 变更）。

## 3. 验收门禁

- 车辆列表可选中 R001（enabled=false V1 只读）→ 监控页 ONLINE 链路实时、
  硬件字段各自 freshness、急停"未验证"、控制按钮禁用。
- 专属实例心跳键与生产实例并存互不掩盖；范围外车辆消息不落库。
- R002 连接/ACL/容器 StartedAt/媒体端口计数与快照一致。
- CI 记录：同 SHA 同 Attempt 10/10 SUCCESS（真实 E2E 非跳过）。

## 4. 回滚边界

- 独立栈 `down` 即回滚（无业务状态卷）；生产栈全程零变更；
  API/Web 回滚=旧镜像标签（无 DB 迁移）。
