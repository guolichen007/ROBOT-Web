# Server/Web 实车 Ready 支线交接

> 回答：Server/Web 接下来从哪里继续。
> 这不是部署文档，也不触发任何服务器 redeploy。

---

## 版本双轨（必须分开）

```text
DEPLOYED_SERVER=41bbaf4398711fd940dde1818193a67d34e355c8   （服务器现场当前实际运行）
SERVER_WEB_CODE_BASE=8e63d5d8def6c60c0244505685e6305422f0cccc （最后含 Server/Web 功能代码的 commit，未部署）
```

- 现场服务器当前仍运行 `41bbaf4`。
- **不要部署 `8e63d5d`。**
- `8e63d5d` 是 **CODE BASE**，不是 branch HEAD；branch HEAD 用 `git rev-parse HEAD` 动态取得，不硬编码。

---

## 支线历史

```text
675b1a6 → 8e63d5d = 7 个 Server/Web code commits
2a9591a / 3defe96 / 最终 closeout fix = docs + HANDOFF closeout layer
```

依序（旧→新，code 层）：

```text
d1bbb2f feat(server-web): real-vehicle-ready fail-closed + multi-vehicle isolation
f13721c fix(server-web): per-command readiness fail-closed + gate csrf/https
5b8f842 fix(gate): prefield/postfield phases + honest write taxonomy
0623c6d fix(gate): bootstrap password change + transport/origin split + diag
4eaeeea test(e2e): tighten multi-vehicle spec to real assertions
c8ae39a fix(test): shared e2e auth helper + gate replay fresh watermark
8e63d5d test(compose): parameterize HTTP test port via TEST_HTTP_PORT
```

完成范围：

```text
- 多车隔离（active vehicle 切换 / 事件 / task / alarm / media）
- per-command readiness fail-closed（patrol/return/stop/estop/reset 各自 readiness）
- control UI + handler fail-closed（含灭火按钮 disabledReason）
- command-dispatcher manual_loop supervisor（pubsub recreate + bounded backoff + manual-heartbeat）
- server_web_gate：prefield/postfield 两阶段 + 诚实写入边界 + CSRF/HTTPS
- 双 mock 车 R002（compose + seed）
- E2E 多车真实断言 + shared auth helper + replay fresh watermark
- TEST_HTTP_PORT 参数化（默认 18080，可设 18081）
```

边界：

```text
BRIDGE_RUNTIME_CHANGED=NO
PROTOCOL_CHANGED=NO
MIGRATION_CHANGED=NO
REAL_CONTROL=NOT_IMPLEMENTED
GITHUB_CI=NOT_TRIGGERED_BY_BRANCH_POLICY   （CI 不监听 integration/**，非失败）
```

---

## 支线冻结（本次 closeout 后）

```text
CURRENT_BRANCH_CLOSEOUT=FROZEN
NO_MORE_CLOSEOUT_COMMITS=YES
```

以后所有现场测试 / Phase E1 数据接入都**新开分支**：

```text
NEXT_DEVELOPMENT_BRANCH=integration/vehicle-data-readonly-v1
```

本任务**不创建**该分支，也不执行 Phase E1。

未来在新分支从最终 closeout SHA 开始：

```text
手动 patrol transport
ROS topic discovery（read-only）
battery / status / location / smoke provider
Web real vehicle UI
```

当前状态：

```text
SERVER_DEPLOYED_SHA=41bbaf4（≠ 8e63d5d）
PR_CREATED=NO
MERGE_PERFORMED=NO
```
