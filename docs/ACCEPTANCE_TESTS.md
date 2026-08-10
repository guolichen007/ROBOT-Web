# Acceptance Tests

## 自动门禁

```powershell
Set-Location C:\Users\13576\Desktop\web_robot
.\scripts\test.ps1
docker compose -f compose.test.yml --profile full up -d --build --wait
$env:E2E_BASE_URL='http://127.0.0.1:18080'
$env:E2E_ADMIN_PASSWORD='TestOnly-ChangeMe-2026!'
docker run --rm --network host -v "${PWD}\apps\web:/app" -w /app mcr.microsoft.com/playwright:v1.54.2-noble npm run test:e2e
```

门禁包括 Ruff、format、mypy、Pytest、Alembic `head→base→head`、schema/model drift、protocol conformance、ESLint、Prettier、Vue typecheck、Vitest、Vite build、Compose config/build/health、Playwright Chromium。

## 浏览器验收

- Login 与首次改密。
- R001 ONLINE、10 Hz 地图位置、heading、传感器和地图 V1。
- 两客户端 lease 互斥；manual hold/move/release；blur/hidden/close 后停止 pulse、发 stop、释放 lease。
- created/published/accepted/executing/succeeded/failed/unconfirmed 分开显示。
- 自动火警、A-12 人工火情、confirm、灭火任务完整状态链。
- map mismatch 拒绝；history/audit/settings 可查；三路视频无源时 OFFLINE。

## 核心故障验收

- Redis、Mosquitto、PostgreSQL 短停：`/health/ready` 在有界时间内 503，恢复后 200。
- API、dispatcher 重启；dispatcher 停止期间 Command+Outbox 保持 QUEUED，恢复后同 command_id 发布并 ACK accepted。
- Mock reboot 后 boot_id 改变，seq 新语境有效，旧租约失效。
- duplicate/out-of-order/bad JSON/invalid schema/skew/late/duplicate/wrong ACK、TTL expiry、map mismatch。
- WS gap 触发 resync；过期命令不重放；manual 不进入 durable outbox。

## Backup/Restore

```powershell
.\scripts\backup.ps1
.\scripts\restore.ps1 -BackupDir '<BACKUP_DIR>' -ConfirmRestore
```

Restore 必须校验 SHA-256，停止 writer，强制重建空 DEV 数据库，恢复 PostgreSQL/asset，升级 migration，核对 users/maps/telemetry/alarms/tasks/audit/commands，再启动全栈。

任何缺失 ACK 或离线发送尝试都不得转为成功；失败测试只能修复或标记真实 BLOCKED。
