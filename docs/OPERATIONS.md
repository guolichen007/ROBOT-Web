# 运维手册

健康端点：`/health/live` 只返回最小存活结果；`/health/ready` 检查 PostgreSQL、Redis、MQTT 与关键进程 heartbeat；`/metrics` 仅管理网/VPN。

结构化日志通过 `docker compose logs --since 30m <service>` 查询。重点关联字段为 request_id、correlation_id、vehicle_id、command_id、task_id、event_id 和 operator_id，日志禁止输出 secret/token。

更新前先备份；拉取已批准 tag，构建镜像，执行 migration，再滚动启动。回滚必须使用上一已验证 tag 和兼容数据库备份，不得直接 reset 数据库。正常停机使用 `docker compose -f docker-compose.server.yml down`，默认保留 volumes。

每次现场变更后检查：ready、R001 heartbeat、MQTT TLS、ACK、media ticket、备份可读性、default partition rows 和磁盘空间。
