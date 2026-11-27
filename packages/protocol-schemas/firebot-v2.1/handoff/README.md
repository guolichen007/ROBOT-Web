# Firebot V2.1 最小场景与任务执行合同 · VM CC 交接（2.1.0-draft.2，未冻结）

机器可读入口：[handoff.json](handoff.json)（Schema 与各向量文件的 SHA256、核心合同清单、规则函数、1.3 线缆映射）。
只需实现 **core** 层即可开始基础导航联调；`DeviceOperation`、`AlarmEvent` 为 extension，**不阻塞**导航联调。
`VehicleSnapshot` 中 `smoke`/`temperature_c`/`devices` 已改为可选，无真实来源时直接不发。

## 车端（SIM 执行适配）必须做到

1. **场景加载**：收到 `SceneLoadRequest` 后，回执 `SceneLoadReceipt` 必须原样回传 `load_id`、本车 `vehicle_id`、当前 `boot_id`，`loaded_at`/`reported_at` 位于请求的 `issued_at..expires_at` 窗口内。用例：`vectors/SceneLoadReceipt.vectors.json#confirm_cases`。
2. **任务反馈**：每条 `MissionFeedback` 带下发时的 `command_id` 与 `execution_gen`；`seq` 在 boot 内单调递增；`waypoint_total` 与下发一致、`waypoint_index` 不回退；`EXECUTING/WAITING_OBSTACLE/SUCCEEDED` 必须带真实 nav `goal_id`，同一航点 goal 不变，换航点可换 goal。用例：`vectors/MissionFeedback.vectors.json#apply_cases`。
3. **状态迁移**：`DISPATCHED→ACCEPTED|REJECTED`；`ACCEPTED→EXECUTING|WAITING_OBSTACLE|PAUSED|FAILED|CANCELLED`；`EXECUTING→…|SUCCEEDED`；不得从 `EXECUTING` 回到 `ACCEPTED`，`ACCEPTED` 不得直达 `SUCCEEDED`。
4. **续巡**：平台以 `execution_gen+1` 和新 `command_id` 重新下发；旧代次/旧 command 的迟到反馈被平台丢弃。
5. **新鲜度**：`observed_at` 距平台时钟超过 10 s 视为过期，超前 2 s 视为未来，均丢弃。

## 需 VM CC 确认（回填即冻结候选）

| # | 问题 | 当前假设 |
|---|---|---|
| Q1 | nav `goal_id` 取值来源（move_base GoalID.id？） | 字符串 ≤128 |
| Q2 | `map_checksum` 算法 | 车端定义，平台只比对 |
| Q3 | 场景下发 topic 采用独立 `scene`/`scene_receipt` 还是 1.4 扩展 | 独立 topic |
| Q4 | 多航点：每个航点一个 nav goal，还是整条路线一个 goal | 每航点一个 |
| Q5 | 加载失败原因码集合 | `^[A-Z][A-Z0-9_]{1,63}$` 自由集 |
| Q6 | 障碍等待由车端判定并上报 `WAITING_OBSTACLE` | 是 |

SIM 车辆仅限 `firebot-sim-*` 命名空间；SIM 的库、Broker、凭证与 REAL 完全分离。样例坐标为合成 Gazebo 场景，不是 R002 现场数据。
