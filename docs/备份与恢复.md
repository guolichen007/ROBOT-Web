# 备份与恢复

DEV 验证命令：

```powershell
.\scripts\backup.ps1
.\scripts\restore.ps1 -BackupDir .\backups\<UTC时间戳> -ConfirmRestore
```

备份覆盖 PostgreSQL custom-format dump、assets、Mosquitto/MediaMTX/Nginx 非 secret 配置、manifest、migration/partition 信息和 SHA256。secret 由部署所有者使用独立加密渠道备份，禁止进入 Git 或普通压缩包。

恢复前校验 SHA256，停止写入服务，重建空数据库，pg_restore，恢复 assets，升级 migration，验证月分区/default rows，再启动全栈。验收必须覆盖 login、map、R001 history、alarms、tasks 和 audit。

RPO/RTO：`TO_BE_CONFIRMED_BY_DEPLOYMENT_OWNER`，不得自行虚构 SLA。
