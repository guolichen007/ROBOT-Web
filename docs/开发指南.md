# 开发规范

- 从最新 `develop` 建短生命周期分支，通过 PR 合入；发布 PR 再由 develop 合入 main。
- 禁止 force push/delete main/develop，禁止绕过 required checks。
- Python 执行 Ruff、format、mypy、Pytest；Web 执行 ESLint、Prettier、vue-tsc、Vitest、build。
- 协议只能修改 canonical JSON Schema，再同步生成模型、Mock、tester、integration package。
- 新业务事实必须在 PostgreSQL commit 后发布实时事件；QoS1 逻辑必须按 at-least-once 与幂等设计。
- 不得在本仓库实现 ROS2、SLAM、Nav2、车端驱动、执行机构或 watchdog。
- production 代码不允许遗留未在 allowlist 中的 TODO/FIXME/HACK。
