# Firebot WEB-R1C 阶段执行报告（控制台重新布局与地图业务闭环）

## 1. 阶段身份

| 项 | 值 |
|---|---|
| ROLE | 桌面 CC（Web/后端研发） |
| STAGE | WEB-R1C（R1C-0..R1C-4） |
| BASE_SHA | `b2e70c621896c23e2904a251d6f13d409173013e`（R1B，已在 origin） |
| CURRENT_SHA | 见 `status.json` / 证据 ZIP 文件名（本报告随最后一个提交一起提交） |
| ENVIRONMENT | 隔离沙箱：PostgreSQL 18.4、Redis 7.2.7、mosquitto 2、Python 3.12、Node 22、Chrome for Testing 158；仅回环 |
| CONTRACT_VERSION | 2.1.0-draft.3（未冻结；R1C 只收紧平台侧规则，版本号不变） |
| REAL_CONTROL | **NO-GO** |
| PRODUCTION_MUTATION | **NONE**（未部署、未执行生产迁移、未恢复真实下行、未改 001 接入与 R002 身份、未新增生产 MQTT 主题） |

## 2. 实际完成（11 个独立提交）

| 提交 | 批次 | 内容 |
|---|---|---|
| 9304c57 | R1C-0 | `scenes/router.py:411/452/484` 复用 `_v2()`（真实查询 + 404），无 ignore/cast；同时修复被 mypy 失败掩盖的潜伏失败（冻结默认开启后 mock 门禁默认码为 `FG_DOWNLINK_FROZEN`） |
| 3dfc6e5 | R1C-1 | OWNER 批准布局：左侧系统设置下方车辆控制面板（设备/手动/监测）、右侧仅双视频纵向原比例、底部固定任务栏（巡检·暂停/继续·目标导航·返航）+ 常驻安全组（停止·软件急停·保护停车）；`useAgentCommands` 单实例；DESIGN.md §3.1 重写；`console-layout.ts` 重写 |
| 0c612f5 | R1C-2 | 统一操作资格：删除 LEGACY 回退；以服务端 `valid_until` 窗口过期；绑定车辆 + boot + 用户权限 + 请求代次；失败即作废；MonitorView 目标导航/灭火只读服务端动作；服务端新增 8 个动作（V1 映射 / Canonical 恒拒并给原因） |
| 3c5d801 | R1C-4 | 5 项合同错误接受独立复现（5/5）→ 11 条 R1C 向量先红后绿 |
| 153ad90 | R1C-3 | 地图文件完整解码（3 项反例 3/3 复现）；导入事务、重复导入、写盘/DB 失败与并发的孤立资产清理 |
| 02a3aba | R1C-3 | 路线航点 arrival 逐点保留（R1B 实现 3/3 红 → 3/3 绿） |
| fd9d1ac | R1C-3 | 迁移 0012 + 场景列表、车位/走廊 CRUD、示教、原子发布、不可变资产、SIM 加载请求 |
| 58881be | R1C-4 | 审查通过的反馈规则接入隔离 SIM ingress 与任务状态持久化 + 真实运行链路 |
| 527489d | R1C-3 | 编辑器前端接入（列表/重开、停靠属性、车位/走廊、示教、校验和、发布） |
| b8a94b7 | R1C-1 | 任务栏状态行常驻占位（终审变异回归中发现的版面跳动）+ 脚本稳定性 |
| （本提交） | — | 本报告 |

## 3. 验证证据

| 门禁 / 测试 | 命令（摘要） | 结果 |
|---|---|---|
| ruff check / format | `ruff check apps/api services scripts` / `--check` | PASS（221 文件） |
| mypy（CI 同款） | `mypy apps/api/app services/protocol.py` + `services/*/main.py` | PASS（78 + 12） |
| 迁移清单 / 矩阵 / 往返 | `check_migration_manifest.py` / `test_migration_matrix.py` / downgrade base→head→seed | PASS（12 版本；6 起点；往返通过） |
| 后端 pytest（CI 目标，按字母序同库分片） | `apps/api/tests services/protocol-tester/tests scripts/test_secret_redaction.py deploy/.../tests` | **795 passed / 1 skipped / 0 failed** |
| 合同向量 | `test_contracts_v21.py` | 223/223（R1C 向量修复前 11 红） |
| 前端 | vue-tsc / eslint 0 warning / prettier / vitest | PASS；vitest **231/231** |
| 布局几何（真实浏览器） | `e2e/current-monitor-layout.spec.ts` 三档 | 3/3 PASS |
| 布局变异 | `e2e/console-layout-mutants.spec.ts` | 9/9 KILLED（含 R1B 横排视频） |
| 状态矩阵（真实栈） | `e2e/console-states.spec.ts`：正常/巡检/火警/离线/冻结 × 1366/1680/1920 | 15 张截图 + 5 份状态 JSON，全部 PASS |
| 业务链 | `e2e/vehicle-agent-mock.spec.ts`（Browser→API→Outbox→Adapter→Mock→ACK→WS） | PASS |
| 资格竞态（真实浏览器） | `e2e/readiness-race.spec.ts` A→B→A + 迟到放宽答复 | R1C 3/3 PASS；R1B 实现 3 次中 2 次红（误启用 开始巡检/停止/软件急停） |
| 资格单元反例 | `tests/operation-readiness-r1c.test.ts` | R1B 9/9 红 → R1C 9/9 绿 |
| 地图编辑器（真实浏览器） | `scripts/r1c-editor-flow.mjs` | 8 步 PASS（中间停靠保存重载保留→车位走廊→发布→列表重开） |
| SIM 运行链路 | `scripts/r1c_sim_chain.py`（真实 API/PG/Redis/mosquitto/ingress/mock-robot） | 17 步 PASS；`R1C_FAR=1` 反向：偏离 3 m 不确认（POSITION_OUT_OF_TOLERANCE） |

## 4. 跨端交接

- 合同：`handoff/README.md` 第 10–12 条（成功确认对 motion 时效/声明后观测、未来位姿、末航点与任务身份的要求；准入时 pose 自身时效）。向量 `R1C_*`。
- SIM 执行端通道：`_sim/v21/{vehicle}/scene_receipt|mission_feedback`（仅 SIM 部署订阅）；加载请求与任务请求通过 HTTP 读取（`POST/GET /api/v1/scenes/...`），平台无新增下行 publish。
- 需 VM CC 确认：`map_checksum` 算法（发布前必须登记）；防火罩驱动与限位反馈合同（设备页签恒显示未接入）。

## 5. 风险及状态

- P0：无。
- P1：GitHub CI 尚未对本批次运行（沙箱无推送权限）；Playwright 截图在 headless Chrome 158 上生成，CI 浏览器版本不同可能有像素差（几何断言含容差）。
- P1：Canonical 暂停巡检无服务端能力，按钮恒禁用并显示 `PATROL_PAUSE_NOT_IMPLEMENTED`；保护停车、车辆启动/关闭（Canonical）、防火罩同样恒禁用并给原因。
- P2：`baseline.spec.ts` 的 WHEP 401 用例在无 MediaMTX 的沙箱中返回 404（R1B 已知环境差异，CI 有媒体栈）。
- 未完成：真实车辆场景加载（REAL 恒拒）；地图可通行性真实规划验证（恒 PENDING_VERIFICATION）。

## 6. 下一阶段

- 最小实施建议：推送后读取 CI；VM CC 回填 `map_checksum` 算法与 SIM 执行端对 `_sim/v21` 的实现；SIM 端到端（Gazebo）联调。
- 需要独立审查：0012 不可变触发器与 CHECK；`sim_v21.vehicle_snapshot` 的静止判定阈值（0.01 m/s、0.01 rad/s）。
- 需要 OWNER 专项授权：任何生产迁移、恢复真实下行、开放场景加载下行主题。
