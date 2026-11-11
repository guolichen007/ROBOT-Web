# VEHICLE_AGENT_V1 host 指标扩展方案（兼容方案，未进入生产）

> 状态：**提案 + 测试完成，默认关闭**。未经验证的新字段不进入生产——
> 本阶段 Agent 默认只把 host 指标写本地日志（`host-metrics.jsonl`），
> 上行 payload 不含任何新字段。

## 1. 背景与原则

G0-A2 只读 Agent 需要采集真实操作系统、主机运行时间、Agent 运行状态、程序存活与
采集时间（任务 A）。现有 `services/vehicle_agent_v1.py` 的 Status/Telemetry Schema
是"最小字段集"，且 `UplinkModel` 配置 `extra="ignore"`（合同注释：忽略扩展元数据，
不赋予其控制含义）。因此新指标进入上行有两层保护：

1. **平台侧**：未知字段被 `normalize()` 丢弃，不会写入业务状态；
2. **车端侧**：本阶段默认不上行（`--host-metrics log-only`）。

本提案给出将来启用上行的兼容路径与验收标准。

## 2. 拟扩展形状（实验性 `host` 字段，仅在 status 上）

```json
{
  "online": true,
  "programs": {"agent": true, "chassis": false, "...": false},
  "task": {"state": "idle", "patrol_next": null, "patrol_count": 0, "patrol_paused": false},
  "ts": "2026-11-11T05:00:00+00:00",
  "host": {
    "agent": {"pid": 1234, "version": "0.1.0"},
    "os": {"name": "Ubuntu", "version_id": "22.04", "pretty_name": "Ubuntu 22.04", "kernel": "5.15.0", "machine": "x86_64"},
    "uptime_seconds": 12345.6,
    "load_average_1_5_15": [0.1, 0.2, 0.3],
    "memory_bytes": {"total": 16777216000, "available": 8388608000},
    "disk_bytes": {"total": 500000000000, "used": 100000000000, "free": 400000000000},
    "programs": {"chassis": false, "navigation": false},
    "collected_at": "2026-11-11T05:00:00+00:00"
  }
}
```

- `programs`（status 顶层，现有 Schema 开放地图）已含 `agent=true`（Agent 自身存活），
  其余程序键沿用平台已知集合（chassis/navigation/patrol/thermal/battery/smog/camera），
  未接入时为 false——这是 Schema 兼容用法，不是扩展。
- `host` 为**可选**新字段，仅在显式 `--host-metrics status-field` 时出现。

## 3. 兼容性分析与验证

| 风险 | 分析 | 验证（测试） |
| --- | --- | --- |
| 平台 normalize 崩溃 | `UplinkModel(extra="ignore")` 显式忽略 | `test_unknown_extra_fields_ignored_by_platform`：`Status.model_validate` 通过，`normalize` 输出不含 `host` |
| 控制含义误用 | 平台对 status 的业务处理只读 `online/programs/task/ts` | `vehicle_agent_ingress.py` 源码复核：不引用 `host` |
| 未验证字段进生产 | 默认 `log-only`，上行为显式开启 | `test_e2e_lifecycle...`：断言上线 status 不含 `host`；`test_no_control_paths_in_source` 兜底 |
| Schema 演进冲突 | 未来正式 Schema 修订以本提案为输入，先加字段再加语义 | 本提案定版后走正常合同变更流程 |

## 4. 启用条件（逐条满足才允许开 `status-field`）

1. 本提案经平台侧评审并入 `vehicle_agent_v1.py` 正式 Schema（带 `host` 字段模型）；
2. 平台 ingress/前端对 `host` 有明确消费需求与验收用例；
3. 灰度观察期（至少 7 天）只读上线，确认无异常事件与数据质量劣化；
4. 满足后由 OWNER 授权，按 SOP 流程逐车开启。

在满足以上条件前，任何部署配置不得使用 `status-field`。
