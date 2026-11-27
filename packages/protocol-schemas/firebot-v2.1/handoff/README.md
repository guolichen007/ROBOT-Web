# Firebot V2.1 最小场景与任务执行合同 · VM CC 交接（2.1.0-draft.3，未冻结）

机器可读入口：[handoff.json](handoff.json)（Schema 与各向量文件的 SHA256、核心合同清单、规则函数、1.3 线缆映射）。
只需实现 **core** 层即可开始基础导航联调；`DeviceOperation`、`AlarmEvent` 为 extension，**不阻塞**导航联调。
`VehicleSnapshot` 中 `smoke`/`temperature_c`/`devices` 已改为可选，无真实来源时直接不发。

## 车端（SIM 执行适配）必须做到

1. **场景加载**：收到 `SceneLoadRequest` 后，回执 `SceneLoadReceipt` 必须原样回传 `load_id`、本车 `vehicle_id`、当前 `boot_id`，`loaded_at`/`reported_at` 位于请求的 `issued_at..expires_at` 窗口内。用例：`vectors/SceneLoadReceipt.vectors.json#confirm_cases`。
2. **任务反馈**：每条 `MissionFeedback` 带下发时的 `command_id` 与 `execution_gen`；`seq` 在 boot 内单调递增；`waypoint_total` 与下发一致、`waypoint_index` 不回退；`EXECUTING/WAITING_OBSTACLE/SUCCEEDED` 必须带真实 nav `goal_id`，同一航点 goal 不变，换航点可换 goal。用例：`vectors/MissionFeedback.vectors.json#apply_cases`。
3. **状态迁移**：`DISPATCHED→ACCEPTED|REJECTED`；`ACCEPTED→EXECUTING|WAITING_OBSTACLE|PAUSED|FAILED|CANCELLED`；`EXECUTING→…|SUCCEEDED`；不得从 `EXECUTING` 回到 `ACCEPTED`，`ACCEPTED` 不得直达 `SUCCEEDED`。
4. **续巡**：平台以 `execution_gen+1` 和新 `command_id` 重新下发；旧代次/旧 command 的迟到反馈被平台丢弃。
5. **新鲜度**：`observed_at` 距平台时钟超过 10 s 视为过期，超前 2 s 视为未来，均丢弃。

## draft.3 对车端的新增要求

6. 每个航点（含 PASS_THROUGH 经过点）按序上报，`waypoint_index` 每次至多 +1；每个航点使用新的 nav `goal_id`，不得复用。
7. 只在最后一个航点上报 `SUCCEEDED`，并附 `final_pose` 与 `stationary: true`；平台还会用自己的快照确认到位与静止。
8. 快照需带实测 `motion`（静止/运动及观测时间），LIVE 位姿需带当前 `map_checksum`。
9. 场景回执只对平台最近一次 `SceneLoadRequest` 有效；之前的回执会被丢弃。用例：`SceneLoadReceipt.vectors.json#state_cases`、`MissionFeedback.vectors.json#success_cases`。

## WEB-R1C 平台侧收紧（合同版本号不变，仍 draft.3；对车端的影响）

10. 成功确认要求 `motion.observed_at` 本身新鲜（≤2 s）、不超前、且晚于 SUCCEEDED 声明时刻；只上报一次“静止”不再长期有效（`MOTION_STALE` / `MOTION_FROM_FUTURE` / `MOTION_PREDATES_CLAIM`）。
11. 位姿观测超前平台时钟 2 s 以上一律不作为证据（`POSE_FROM_FUTURE`）；准入时位姿观测本身须 ≤5 s，快照新生成但位姿旧同样拒绝（`POSE_STALE`）。
12. SUCCEEDED 必须出现在下发路线的最后一个航点（`waypoint_index` 等于路线末点 index，`waypoint_total` 与路线长度一致），且 mission/command/代次/车辆/环境与请求完全一致（`TARGET_WAYPOINT_NOT_REACHED` / `MISSION_IDENTITY_MISMATCH`）。用例：`MissionFeedback.vectors.json#success_cases` 与 `OperationReadiness.vectors.json#admission_cases` 中 `R1C_*`。

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
