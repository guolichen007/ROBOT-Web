# 模拟数据上线前清理方案

## 现状审计结论

数据模型（`apps/api/app/db/models.py`）**没有**统一的 `source_kind`/`simulated` 判别字段：

| 表 | 可用判别信号 |
|---|---|
| `Task` | `parameters_json.source`（如 `OPERATIONS_HMI`）、`created_by` |
| `FireEvent` | `detection_method`（AUTO/MANUAL）、`note`、`fingerprint` |
| `AuditLog` | `action`、`ip`、`user_agent`（无来源字段，不可靠） |
| `TelemetrySample` / `SensorSample` | `boot_id`（seed 数据为 `seed-history`；mock 运行时为随机 UUID） |
| `RobotIntegrationProfile` | `source_kind`（`MOCK` / `CANONICAL_MQTT` / `ROS_COMPAT`）——**最可靠** |

**可靠判别器**：`RobotIntegrationProfile.source_kind == "MOCK"` 的机器人，其任务/火情/遥测都是模拟数据。

**不可靠的部分**：没有该标记的旧数据（如人工上报火情、历史测试任务）无法用日期/编号可靠区分，**不猜测删除**。

## 推荐流程

1. **识别**：以 `source_kind == "MOCK"` + `boot_id == "seed-history"` + 明确测试 note 标记为准。
2. **dry-run**：先数数量，不删。
3. **备份**：`--export` 导出 JSON 备份。
4. **删除**：`--execute`，仅 `app_env == "dev"` 时放行（server 需 `--force-server`）。
5. **审计日志不删除**（immutable/retention 合同）。

## 命令

```bash
# 在 api 的 venv 内，从仓库根目录执行
python scripts/reset_demo_data.py                  # dry-run
python scripts/reset_demo_data.py --export ./backup # dry-run + 备份
python scripts/reset_demo_data.py --execute         # DEV 环境删除
```

## 生产切换说明

真实上线建议**使用全新生产数据库 / 新 site**，而不是在含模拟数据的老库上做删除；历史模拟审计通过 UI 过滤“开发测试”保留可追溯性。若必须复用老库，先 dry-run + 人工确认名单，再执行。
