# WEB_V2_1_REQUIREMENTS — Firebot Web V2.1 需求基线

> 基线 `23854b0`（feature/web-r1-min）· 分支 `feature/firebot-r002-web-v2-1` · R002=`firebot-vehicle-02` · Canonical MQTT 1.3/8883 · 001 独立接入不变。
> 设计：[DESIGN.md](../../../DESIGN.md) · 合同：[R002_SCENE_CONTRACT](../../技术合同/web-v2.1/R002_SCENE_CONTRACT.md)、[R002_OPERATION_CONTRACT](../../技术合同/web-v2.1/R002_OPERATION_CONTRACT.md) · 动作：[WEB_V2_1_ACTION_MATRIX](WEB_V2_1_ACTION_MATRIX.md) · 验收：[R002_ACCEPTANCE_MATRIX](R002_ACCEPTANCE_MATRIX.md)。

## 1. 授权边界（本轮）

批准：本地研发、隔离测试、SIM 演练。不批准：生产发布、推送正式分支、修改 Release、恢复真实下行、启用真实定时任务。暂缓：自动精确停靠、自动防火罩作业、拖车、移动端扩展。禁止：将演示停车场（DEMO_PARKING）固定坐标用于真实 R002；以临时接口或前端内存模拟冒充真实能力。

## 2. 需求（编号用于矩阵追溯）

| ID | 需求 | 阶段 | 依赖合同 | 本轮状态 |
|---|---|---|---|---|
| RQ-01 | 综合控制台重设计：地图主导、视频辅助、紧凑状态栏、任务操作稳定可见 | WEB-1 | VehicleSnapshot | 设计定稿；实现待 WEB-1 |
| RQ-02 | 车辆选择 + 环境标识（REAL/SIM 徽标） | WEB-1 | envBinding | 设计定稿 |
| RQ-03 | 一键启动/关闭 + 设备健康 | WEB-1 | OperationReadiness.start/shutdown_vehicle、devices[] | 合同 DRAFT |
| RQ-04 | 巡检、暂停、继续、目标导航、返航、取消 | WEB-1/3 | MissionRequest/Feedback | 合同 DRAFT |
| RQ-05 | 人工控制 + 独占控制权显示 | WEB-1 | control_owner、manual_control | 合同 DRAFT；`ManualControl.vue` 未挂载 |
| RQ-06 | 固定停止入口 + 独立保护停车入口（不随页签消失） | WEB-1 | stop_motion、protective_stop | 合同 DRAFT |
| RQ-07 | 真实 X/Y/yaw + 数据时间 + 质量 | WEB-1 | VehicleSnapshot.pose | 合同 DRAFT |
| RQ-08 | 火情弹窗 + 地图定位；故障原因与控制资格解释 | WEB-1/3 | AlarmEvent、reasons | 合同 DRAFT |
| RQ-09 | 防火罩人工操作面板 + 真实反馈（限位确认） | WEB-1 | DeviceOperation | 合同 DRAFT；无车端驱动 |
| RQ-10 | PGM/PNG + YAML 安全导入、元数据解析 | WEB-2 | SceneManifest.nav_map | **前端解析库已实现** |
| RQ-11 | 世界↔屏幕双向变换（含 resolution/origin/yaw、y 翻转） | WEB-2 | 地图坐标合同 | **已实现+测试** |
| RQ-12 | 巡检/消防作业/返航点 CRUD、拖动、旋转、精确输入、当前位置示教 | WEB-2 | scenePoint | 编辑器模型已实现（纯函数），UI 待做 |
| RQ-13 | 车位区域与编号、路线排序与方向预览、右车道走廊 | WEB-2 | parking_slots/routes/corridors | 模型已实现；UI 待做 |
| RQ-14 | 撤销/重做、草稿保存、发布校验 | WEB-2 | SceneManifest.validation | 撤销/重做已实现；服务端校验规则已实现（库） |
| RQ-15 | 导航地图与业务场景版本分离；发布与车端加载独立；无回执不得作为真实任务依据 | WEB-2 | SceneLoadReceipt | 规则已实现（库）；DB 迁移待审查 |
| RQ-16 | 修复地图更新跨版本迁移；禁止改 PUBLISHED | WEB-2 | — | **已修复+反例测试** |
| RQ-17 | 任务唯一身份、幂等、已发布路线快照 | WEB-3 | MissionRequest | 复用现有幂等 |
| RQ-18 | 障碍等待、人工暂停/恢复、真实 Goal 与终态关联、真实停车确认、失败/取消 | WEB-3 | MissionFeedback、NavigationDiagnostics | 合同 DRAFT |
| RQ-19 | 设备健康与观测时间；电量 50%、温度 80℃、烟雾 300ppm（单位验证后） | WEB-3 | AlarmEvent.measurement | 合同 DRAFT |
| RQ-20 | 告警确认与处理记录 | WEB-3 | AlarmEvent.handling | 复用 fire_events |
| RQ-21 | 定时任务配置、预演、执行历史；保持 PREVIEW_ONLY | WEB-3 | — | 现有四层保护保持不动 |
| RQ-22 | 车云 SIM 联调（真实 API/PG/Broker/WS） | WEB-4 | 全部 | 待 VM |
| RQ-23 | dispatcher 最终门禁覆盖 Outbox、Redis 人工控制、Redis 安全 Stream | 横切 | OPERATION §7 | **规格已出，待审查** |

## 3. 非功能

1366×768 与 1920×1080 两档无横向滚动、关键操作不出折叠线；所有数据区四态；`:focus-visible` 全覆盖；中文文案；未知值显示 `--`；时间显示本地时区并可悬停看 ISO；任何下发类动作在 SIM 验证前不得在 REAL 启用。
