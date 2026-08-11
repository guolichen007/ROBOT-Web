# 数据库与时间分区

PostgreSQL 是业务事实最终来源。所有时间使用 `timestamptz`/UTC。核心域包括 auth、site/map version、robot/boot session、manual session、task/event、command/outbox、alarm、telemetry/sensor、audit/media/settings。

`telemetry_samples` 与 `sensor_samples` 按 `server_received_at` 建 `YYYY_MM` 月分区。migration 和 worker 保证当前月及未来两个月存在。default partition 仅异常兜底；其行数通过 `firebot_partition_default_rows` metric 暴露，非零会记录 warning。

retention worker 以删除过期月分区维护 telemetry 30 天、sensor 90 天；audit 365 天按有界批次维护。任务、命令和火情不自动删除。跨月、retention 与 restore 均有测试。
