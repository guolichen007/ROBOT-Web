# 稳定性测试报告

执行日期：2026-08-11（UTC）

环境：`compose.test.yml`，独立 PostgreSQL/Redis/Mosquitto，Mock 停止后由 protocol tester 通过真实 MQTT 发布。

结论：**PASS**。本报告只证明本机基础稳定性，不声明正式容量 SLA。

## R001 一小时 Soak

精确命令：

```powershell
docker compose -f compose.test.yml run --name firebot-r001-soak --rm tests python services/protocol-tester/load.py --mode soak --duration 3600
```

| 项目 | 结果 |
| --- | --- |
| 开始 UTC | `2026-08-11T04:40:07.671700Z` |
| 结束 UTC | `2026-08-11T05:46:48.399572Z` |
| 逻辑负载时长 | 3600 秒；实际 wall time 4000.73 秒（每轮发布耗时计入） |
| location | 36,000（目标 10 Hz × 3600） |
| heartbeat | 3,600（目标 1 Hz × 3600） |
| status | 3,600（目标 1 Hz × 3600） |
| sensor | 7,200（目标 2 Hz × 3600） |
| availability / capabilities | 2 / 1 |
| 发布失败 | 0 |
| 容器退出码 | 0 |
| API/ingress/dispatcher/worker/PostgreSQL/Redis 重启 | 全部 0 |
| outbox pending | 0 |
| realtime stream | 10,003；配置约 10,000 的 approximate maxlen，保持有界 |
| safety command stream | 0 |
| telemetry / sensor 总行数 | 10,339 / 21,188（含 seed 与 burst；平台按配置降采样） |
| telemetry / sensor default partition | 0 / 0 |
| 月分区 | 2026-08、2026-09、2026-10 均存在 |

### 资源趋势

| 服务 | 开始内存 | 结束内存 |
| --- | ---: | ---: |
| API | 75.43 MiB | 75.44 MiB |
| MQTT ingress | 62.44 MiB | 62.19 MiB |
| Command dispatcher | 58.66 MiB | 58.66 MiB |
| Task worker | 54.06 MiB | 54.14 MiB |
| PostgreSQL | 118.30 MiB | 137.50 MiB |
| Redis container | 11.05 MiB | 18.98 MiB |

Redis 高频去重键按 topic 使用 120/600 秒 TTL。10 车 burst 结束时为 3,027 keys / 10.86 MiB，全部高频窗口过期后为 400 keys / 10.56 MiB；Redis Stream 保持在约 10,000 条的有界窗口。未观察到应用进程或去重键的持续线性增长。PostgreSQL 增长与本轮写入的分区数据一致。

## 多车 Burst

精确命令：

```powershell
docker compose -f compose.test.yml run --name firebot-ten-robot-burst --rm tests python services/protocol-tester/load.py --mode burst --duration 600
```

| 项目 | 结果 |
| --- | --- |
| 开始 UTC | `2026-08-11T04:40:08.209783Z` |
| 结束 UTC | `2026-08-11T04:51:15.419158Z` |
| robots | 10 |
| location | 60,000（10 × 10 Hz × 600 秒） |
| heartbeat / status | 6,000 / 6,000 |
| sensor | 12,000 |
| availability / capabilities | 20 / 10 |
| failures | 0 |
| 容器退出码 | 0 |
| 结果 | **PASS** |

## Gate 结论

- 无进程崩溃或重启。
- 无 outbox 异常积压。
- Redis realtime stream 与高频去重键均保持有界。
- telemetry/sensor 进入真实月分区，default partition 为零。
- 应用内存无明显持续线性增长。
- 一小时 soak 与 10 车 burst 均为 **PASS**。
