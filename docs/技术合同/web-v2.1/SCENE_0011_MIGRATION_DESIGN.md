# SCENE 0011 · 地图场景持久化最小数据模型与迁移设计（仅设计，未实施）

> 状态：**第一批已在隔离库实施（WEB-R1B，`20261127_0011_scene_v21`，PostgreSQL 18.4 验证）**；未执行生产迁移。下文 §2 为完整设计，§6 为本批实际落地与偏差。
> 依据：[R002_SCENE_CONTRACT.md](R002_SCENE_CONTRACT.md)（2.1.0-draft.2）· 现有最新迁移 `20261122_0010_web_r1_min`。

## 1. 目标与非目标

**目标**：让 `/maps/editor` 的草稿能落库；发布时计算并冻结 digest；记录车端加载请求与回执，使 `scene_load_confirms` / `mission_admissible` 有持久化事实来源。
**非目标（本迁移不做）**：改造任务创建、删除旧表、回填历史 PUBLISHED 版本 digest、任何下行。

## 2. 最小新增（只加不改，旧表全部保留）

| 表 / 列 | 关键字段 | 约束 | 对应合同 |
|---|---|---|---|
| `map_versions` + 列 | `revision INT NOT NULL DEFAULT 1`、`nav_image_sha256 CHAR(64)`、`nav_yaml_sha256 CHAR(64)`、`nav_map_checksum VARCHAR(128)`、`width_px INT`、`height_px INT`、`occupied_thresh/free_thresh FLOAT`、`negate SMALLINT`、`traversability VARCHAR(24) DEFAULT 'PENDING_VERIFICATION'`、`digest CHAR(64)` | CHECK `traversability IN ('VERIFIED','PENDING_VERIFICATION','FAILED')`；CHECK `status<>'PUBLISHED' OR digest IS NOT NULL`（仅对新发布生效，见 §4） | SceneManifest.nav_map / validation / digest |
| `scene_points`（新） | `id`, `map_version_id FK CASCADE`, `code`, `kind`, `x,y,yaw`, `xy_tolerance_m`, `yaw_tolerance_rad`, `parking_slot_id FK NULL`, `taught_from` | UNIQUE(`map_version_id`,`code`)；CHECK kind 枚举；CHECK yaw ∈ [-π, π] | points[] |
| `scene_routes`（新） | `id`, `map_version_id FK CASCADE`, `code`, `closed` | UNIQUE(`map_version_id`,`code`) | routes[] |
| `scene_route_points`（新） | `route_id FK CASCADE`, `seq INT`, `point_id FK **RESTRICT**` | PK(`route_id`,`seq`) | point_refs（删除被引用点位由 DB 拒绝） |
| `scene_corridors`（新） | `id`, `map_version_id FK CASCADE`, `code`, `kind='RIGHT_LANE'`, `centerline_json`, `width_m` | CHECK width 0.5–10 | corridors[] |
| `scene_load_requests`（新） | `load_id PK`, `robot_id FK`, `map_version_id FK`, `target_boot_id`, `scene_digest`, `nav_map_checksum`, `issued_at`, `expires_at`, `issued_by` | — | SceneLoadRequest |
| `scene_load_receipts`（新） | `receipt_id PK`, `load_id FK`, `robot_id FK`, `boot_id`, `seq`, `result`, `reason_code`, `loaded_at`, `reported_at`, `confirmed BOOL`, `confirm_code` | UNIQUE(`load_id`)（一次加载一个最终回执） | SceneLoadReceipt + `scene_load_confirms` 结果 |
| `robots` + 列 | `loaded_scene_version_id FK NULL`、`loaded_scene_digest CHAR(64) NULL`、`loaded_scene_boot_id VARCHAR(36) NULL` | 仅由回执确认写入；boot 变化时由 ingress 清空 | VehicleSnapshot.scene |

**跨版本约束在 DB 层兜底**：`scene_points.parking_slot_id` 与 `scene_route_points.point_id` 通过触发器或复合外键 `(map_version_id, id)` 保证同版本（实现时二选一，推荐复合外键：在被引用表加 UNIQUE(`map_version_id`,`id`)）。

## 3. 并发

草稿保存：`UPDATE map_versions SET revision=revision+1 WHERE id=? AND revision=? AND status='DRAFT'`，影响 0 行 → 409 `REVISION_CONFLICT`（沿用 0010 计划模块已验证的模式）。发布：同一语句附加 `status='DRAFT'` 并在同事务内写 digest，二次发布 0 行即 409。

## 4. 兼容与回滚

- 旧 `inspection_points / extinguish_points / trajectories` 不动；新编辑器只写新表。读取端以“新表优先、旧表回退”的视图映射，旧版本照常显示。
- 已 PUBLISHED 的旧版本 `digest` 允许为 NULL（CHECK 以 `published_at >= 迁移时刻` 为条件，或用 NOT VALID 约束），**不回填**；需要回填时另起只读核对脚本 + 单独审批。
- downgrade：只 DROP 新表与新列；不触碰旧数据。迁移 manifest（`migration-sha256.json`）同步更新。
- 生产执行需 OWNER 授权；先在隔离库跑 `test_migration_matrix`。

## 5. 实施拆分（建议 3 个独立提交）

1. 0011 迁移 + 模型 + migration matrix 测试（无 API 变化）。
2. 草稿 CRUD/保存/发布 API（revision + digest + 发布校验调用 `contracts_v21`）。
3. 加载请求/回执落库 + `robots.loaded_scene_*` 写入（只在 SIM 验证；REAL 恒 NONE）。


## 6. WEB-R1B 第一批实际落地

| 项 | 实施 |
|---|---|
| 存储版本 | `map_versions.storage_version` 1/2，CHECK 约束；既有数据全部 =1，旧表与历史数据保留；不按新表是否为空隐式回退 |
| 唯一写入权威 | storage_version=2 只能经 `/api/v1/scenes` 写；旧 `/parking-slots` 等写接口返回 409 `LEGACY_WRITE_ON_V21_SCENE`；新接口对 =1 版本返回 409 `LEGACY_STORAGE_VERSION` |
| 跨版本引用 | 复合外键 `(map_version_id, point_uid)` / `(map_version_id, route_uid)` / `(map_version_id, parking_slot_id)`；绕过 API 的原生 SQL 也被拒绝（测试） |
| 被引用点位删除 | 点→路线引用外键为 `DEFERRABLE INITIALLY DEFERRED`。**偏差**：设计稿原写 NO ACTION，实测在 PG 上整版本级联删除会误报违反（测试清理发现），已改为提交时检查 |
| 并发 | `UPDATE ... WHERE revision=? AND status='DRAFT' AND storage_version=2 RETURNING revision` 与保存同事务；双线程并发保存严格 `[200, 409]` |
| 导入 | YAML+PGM/PNG 服务端严格校验（与前端同规则），图像沿用 API 10 MiB 上限；图像/YAML 入 assets，sha256 落库；`nav_map_checksum` 待车端算法，暂空 |
| 本批未做 | 原子发布与确定性场景资产摘要、车位/走廊持久化、加载请求/回执落库、`robots.loaded_scene_*` |
