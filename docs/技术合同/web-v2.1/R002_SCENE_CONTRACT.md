# R002_SCENE_CONTRACT — 地图场景、发布与车端加载合同

> CONTRACT_VERSION=`2.1.0-draft.3` · 状态：**DRAFT，未冻结**（须独立审查后冻结） · 基线 `23854b0`
> 机器可读：[firebot-web-v2.1.contracts.schema.json](../../../packages/protocol-schemas/firebot-v2.1/firebot-web-v2.1.contracts.schema.json)（`$defs/SceneManifest`、`$defs/SceneLoadReceipt`）；样例/向量：[vectors/](../../../packages/protocol-schemas/firebot-v2.1/vectors/)；语义规则：[contracts_v21.py](../../../services/contracts_v21.py)。
> 上位合同：[地图坐标合同.md](../地图坐标合同.md)、[MQTT协议.md](../MQTT协议.md)、[FIREBOT_BRIDGE_CONTRACT_1.3.md](../FIREBOT_BRIDGE_CONTRACT_1.3.md)。冲突时上位合同优先。

## 0. draft.2 变更（HF1，2026-11-27）

- 新增 `SceneLoadRequest`（平台→车，含 `load_id`、`target_boot_id`、`issued_at..expires_at` 窗口）。
- `SceneLoadReceipt` 必须回传 `load_id`；`scene_load_confirms(manifest, request, receipt, current_boot_id)` 额外校验：回执车辆=请求车辆、环境一致、`load_id` 一致、请求与 manifest 指向同一场景、`loaded_at`/`reported_at` 在加载窗口内（修复审查反例 CE3）。
- 任务准入：ARCHIVED/DRAFT 场景一律 `SCENE_NOT_ACTIVE`（修复 CE5）。
- 交接包：[handoff/](../../../packages/protocol-schemas/firebot-v2.1/handoff/README.md)。

## 1. 两个版本，彼此独立

**导航地图（nav_map）**：车端 SLAM/定位使用的栅格（PGM/PNG + ROS map_server YAML），身份 = `map_code + map_version + map_checksum`（1.3 location 已上报这三项）。**业务场景（scene）**：平台维护的点位、车位、路线、右车道走廊，身份 = `scene_id + scene_version + digest`。一个场景版本绑定且仅绑定一个导航地图身份。导航地图更新必须产生新场景版本。

## 2. 生命周期

```
DRAFT --(校验通过+发布权限)--> PUBLISHED --(车端 SceneLoadReceipt LOADED 且匹配)--> 对该车 LOADED_CONFIRMED
  │                                │
  └─ 可编辑（乐观锁）              └─ 不可修改；变更=复制为新 DRAFT（scene_version+1）
PUBLISHED --> ARCHIVED（不可再被新任务引用）
```

规则：
1. PUBLISHED/ARCHIVED 版本任何子资源不可增删改（API 409）。
2. 子资源永远不能在版本间迁移；更新请求中的 `map_version_id` 必须等于资源当前版本，否则 409 `CROSS_VERSION_MOVE_FORBIDDEN`（已在本轮修复，见实施报告）。
3. 引用必须同版本：点位的 `parking_slot_id` 必须属于同一版本，否则 422 `SLOT_VERSION_MISMATCH`（已修复）。
4. 发布 ≠ 下发 ≠ 运动授权。发布只冻结内容并计算 digest；车端加载由独立流程推送并以回执确认；任务能否执行仍由 OperationReadiness 单独决定。
5. 没有匹配回执（`scene_load_confirms` 返回 OK）时，该场景**不得**成为真实任务依据（`mission_admissible` → `SCENE_NOT_LOADED_ON_VEHICLE`）。

## 3. 发布前校验（服务端执行，前端仅展示）

| 校验 | 失败代码 | 实现 |
|---|---|---|
| 导航地图身份完整（code/version/checksum/image+yaml sha256） | SCHEMA_INVALID | schema `nav_map` |
| 点位坐标/姿态有限且在范围内，frame=map | SCHEMA_INVALID / NON_FINITE_NUMBER | schema + `_assert_finite` |
| 点位/路线/走廊/车位引用一致，无重复编号 | SCENE_REFERENCE_INVALID | `scene_reference_issues` |
| 存在返航点 | RETURN_HOME_POINT_MISSING | 同上 |
| 跨版本引用 | 结构上不可能（引用为同 manifest 内 uuid） | — |
| 车体可通行性 | `traversability`=PENDING_VERIFICATION | **无真实规划证据禁止 VERIFIED**（schema 强制 evidence） |
| 路线方向/走廊关系 | ROUTE_CORRIDOR_REF_MISSING / CORRIDOR_ZERO_LENGTH_SEGMENT | 同上；右侧关系的几何检查待车端提供车体尺寸后补 |
| digest 与内容一致 | SCENE_DIGEST_MISMATCH | `scene_digest`（排序键、无空白、UTF-8 的 sha256） |
| 并发编辑/发布冲突 | 409 REVISION_CONFLICT | `base_scene_version`/revision 条件更新（实现待 DB 迁移审查，§6） |

## 4. 原始地图导入安全（WEB-2 实施约束）

YAML 用严格白名单解析（不使用通用 YAML 解析器；前后端同规则：`apps/web/src/lib/nav-map.ts`、`apps/api/app/modules/scenes/navmap.py`）；YAML ≤ 64 KiB、图像 ≤ 10 MiB（R1B 与 API 上传上限统一，取得真实地图后再评估）、像素宽高各 ≤ 8192 且总像素 ≤ 32M；`image:` 只允许与上传包同目录的文件名（拒绝绝对路径、`..`、URL、符号链接）；`resolution`>0 且 ≤1、`origin` 三元有限数、`occupied_thresh`>`free_thresh`、`negate`∈{0,1}；拒绝 NaN/Infinity、重复键、YAML 别名炸弹。前端解析仅用于预览，服务端必须重复校验。

## 5. 字段映射（复用现有表）

| 合同字段 | 现有实现 | 差距 |
|---|---|---|
| scene_id / scene_version | `map_versions.id` / `map_versions.version`(String) + `semantic_revision` | version 为字符串；需整数单调或映射规则 |
| status | `map_versions.status` DRAFT/PUBLISHED/ARCHIVED | 一致 |
| digest | `map_versions.checksum`（创建时写 `""`，从不计算） | **P1：发布不计算摘要** |
| nav_map.* | `width_m/height_m/origin_x/origin_y/rotation_rad/resolution_m_per_pixel/frame_id/background_asset_id` | 缺 map_code 绑定、image/yaml sha256、阈值、像素尺寸 |
| nav_map.map_checksum | 1.3 `location.map_checksum` | ingress **未持久化、未比对**（P1） |
| points (INSPECTION) | `inspection_points.pose_json`（theta） | 无 code；`theta`→`yaw` 映射 |
| points (FIRE_OPERATION) | `extinguish_points.pose_json` | 同上 |
| points (RETURN_HOME/WAITING) | `navigation_presets` / route_builder 硬编码 `(1.2,1.2,π/2)` | **禁止用于 R002**；需场景内显式点位 |
| parking_slots | `parking_slots.polygon_json/center_pose_json/code` | 一致 |
| routes | `trajectories.path_json`（坐标列表，非点位引用） | 需点位引用模型 |
| corridors (RIGHT_LANE) | route_builder 内写死走廊图 | 无存储 |
| validation | 无 | 新增 |
| SceneLoadReceipt | 无；1.3 无此消息 | 需车端新增上行（见 §7） |
| vehicle loaded scene | `robots.current_map_version`（仅导航地图版本） | 需 loaded_scene_* |

## 6. 数据库迁移影响（未执行，待审查）

建议迁移 `20261127_0011_scene_v21`（**本轮未提交**）：`map_versions` 增加 `revision INT NOT NULL DEFAULT 1`、`nav_image_sha256`、`nav_yaml_sha256`、`nav_map_checksum`、`occupied_thresh`、`free_thresh`、`negate`、`width_px`、`height_px`、`traversability`；新增 `scene_points`（统一点位，含 kind/code/pose/tolerance）、`scene_routes`、`scene_route_points`（有序引用 + FK ON DELETE RESTRICT，实现“删除被引用点位拒绝”）、`scene_corridors`、`scene_load_receipts`（receipt_id 唯一、vehicle_id、boot_id、scene_version_id、digest、result、reason_code）；`robots` 增加 `loaded_scene_version_id`、`loaded_scene_digest`、`loaded_scene_boot_id`。兼容：旧 `inspection_points/extinguish_points/trajectories` 保留只读并由视图映射，不删除；PUBLISHED 旧版本 digest 回填仅在只读校验后执行。

## 7. 车端交接（需 VM CC / 车端输入）

1. 场景下发通道：建议 `robot/{vehicle_id}/scene`（下行，QoS1，非 retain）与 `robot/{vehicle_id}/scene_receipt`（上行，QoS1）。这是 **MQTT 1.3 的扩展**，需要 schema 1.4 或独立 topic 合同；**未冻结**。
2. 车端需回答：map_checksum 的确切算法；导航地图文件是否随场景下发还是车端本地已有；车体外廓尺寸与最小转弯半径（可通行性检查）；加载失败原因代码集。
3. 在确认前，平台只在 SIM 环境演练 `SceneManifest → SceneLoadReceipt`，REAL 车辆 `load_state` 恒为 `NONE`。

## 8. 不兼容影响

新 schema 为平台内合同，不改变现有 1.3 线缆格式，不影响 001 独立接入。新增 topic、schema 版本或 DB 迁移均需独立审查后再实现。
