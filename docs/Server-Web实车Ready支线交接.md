# Server/Web 实车 Ready 支线交接

> 回答：Server/Web 接下来从哪里继续。
> 这不是部署文档，也不触发任何服务器 redeploy。

---

## 版本双轨（必须分开）

```text
DEPLOYED_SERVER=41bbaf4398711fd940dde1818193a67d34e355c8   （服务器现场当前实际运行）
SERVER_WEB_CANDIDATE=8e63d5d8def6c60c0244505685e6305422f0cccc （开发候选，未部署）
```

- 现场服务器当前仍运行 `41bbaf4`。
- **不要部署 `8e63d5d`。**
- 本轮文档同步不触发 server redeploy。

---

## 支线历史

```text
BASE=675b1a6ee9259e669b175e49e65f94462548690b
CURRENT=8e63d5d8def6c60c0244505685e6305422f0cccc
AHEAD_BY=7
```

依序（旧→新）：

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
```

---

## 下一步（不要在本次任务内执行）

```text
1. 宿主验证 18080/18081 端口后跑 docker 双车 + server_web_gate local-sim + Playwright
2. command-dispatcher Redis 断连 live 实测
3. 等车端 R0-R4 完整 PASS
4. PR-A：ui/youdao-light-hmi-v1 → develop（CI，暂不 merge）
5. 本支线 rebase 最新 develop 后再跑全 CI
6. 产出不可变 SERVER_WEB_READY_RC_SHA 才进入部署候选
```

当前状态：

```text
SERVER_DEPLOYED_SHA=41bbaf4（≠ 8e63d5d）
PR_CREATED=NO
MERGE_PERFORMED=NO
```
