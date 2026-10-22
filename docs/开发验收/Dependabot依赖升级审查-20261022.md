# Dependabot 依赖升级审查 — 2026-10-22

本次审查基于 feature/vehicle-agent-hmi-v1，BASE_SHA=5040468c5fcb34725ee44d581a68b802306b1db8。直接在当前工作树重放允许的升级意图，不 merge/cherry-pick 历史 Dependabot 分支，不改 main 或生产。

INTEGRATED 表示本轮候选已纳入升级；最终通过情况与分支清理结果以同一最终 HEAD 的完整 CI 和验收报告为准。只有 9/9 CI success 后才关闭旧 PR 和删除下列精确分支；不得因更新类型已审查而提前清理。

## 11 项决策与后续验收

| PR | 依赖 | 原版本 | 旧 PR 目标 | 结果 | 决策及后续要求 |
|---|---|---|---|---|---|
| [#1](https://github.com/guolichen007/ROBOT-Web/pull/1) | actions/setup-node | 5.0.0 | 7.0.0 | INTEGRATED | Node 24 action runtime；保留 node-version=24.19.0 和 npm cache。 本轮须通过完整 CI。 |
| [#2](https://github.com/guolichen007/ROBOT-Web/pull/2) | actions/upload-artifact | 4.6.2 | 7.0.1 | INTEGRATED | 保留 name/path/if-no-files-found/retention-days。 本轮须通过完整 CI。 |
| [#3](https://github.com/guolichen007/ROBOT-Web/pull/3) | actions/checkout | 4.2.2 | 7.0.1 | INTEGRATED | 覆盖全部 workflow 的旧 pin。 本轮须通过完整 CI。 |
| [#4](https://github.com/guolichen007/ROBOT-Web/pull/4) | vitest | 3.2.7 | 4.1.10 | DEFERRED | test runner major；S5 前不升级 Vitest 4。 后续维护必须完成下述全部验收。 |
| [#5](https://github.com/guolichen007/ROBOT-Web/pull/5) | typescript | 5.9.2 | 7.0.2 | DEFERRED | compiler 跨 major；S5 前不升级 TypeScript 7。 后续维护必须完成下述全部验收。 |
| [#6](https://github.com/guolichen007/ROBOT-Web/pull/6) | vite | 7.3.6 | 8.2.1 | DEFERRED | build system major；S5 前不升级 Vite 8。 后续维护必须完成下述全部验收。 |
| [#7](https://github.com/guolichen007/ROBOT-Web/pull/7) | axios | 1.18.0 | 1.19.0 | DEFERRED | 旧目标已过时；REASSESS_LATEST_COMPATIBLE_1X，按 fresh production audit 重评兼容 1.x。 后续维护必须完成下述全部验收。 |
| [#8](https://github.com/guolichen007/ROBOT-Web/pull/8) | vue-tsc | 3.0.5 | 3.3.9 | INTEGRATED | 正常 npm install --save-dev --save-exact；当前 HEAD 重生成 lockfile。 本轮须通过完整 CI。 |
| [#9](https://github.com/guolichen007/ROBOT-Web/pull/9) | pinia | 3.0.3 | 4.0.2 | DEFERRED | runtime major；S5 前不升级 Pinia 4。 后续维护必须完成下述全部验收。 |
| [#10](https://github.com/guolichen007/ROBOT-Web/pull/10) | actions/setup-python | 5.4.0 | 7.0.0 | INTEGRATED | 保留 python-version/cache；仓库未用 pip-install input。 本轮须通过完整 CI。 |
| [#11](https://github.com/guolichen007/ROBOT-Web/pull/11) | github/codeql-action | 4.37.6 | 4.37.7 | INTEGRATED | init 与 analyze 同时升级到相同完整 SHA。 本轮须通过完整 CI。 |

Pinia 4、TypeScript 7、Vite 8、Vitest 4 均不进入 S5 前候选。Axios 1.19.0 未整合，也不视作当前安全修复目标；继续保留 1.18.0，本轮不机械追随旧 PR。后续状态：**REASSESS_LATEST_COMPATIBLE_1X**，重新评估当前兼容 1.x、变更说明与 fresh production npm audit 后决定版本。

所有延期项在 S5 稳定后的前端依赖维护阶段逐个升级，先移除相应 semver-major ignore；不得把多项 major 叠在一个无法定位问题的变更中。每项必须重新运行 npm audit production/full、lint、format、typecheck、unit、build、legacy E2E、vehicle-agent E2E 和 HMI 回归，保留证据。禁止 npm audit fix 和 npm audit fix --force。

## Actions 完整 pin 与输入兼容

| Action | 本轮完整 SHA | 版本 |
|---|---|---|
| actions/checkout | 3d3c42e5aac5ba805825da76410c181273ba90b1 | 7.0.1 |
| actions/setup-node | 820762786026740c76f36085b0efc47a31fe5020 | 7.0.0 |
| actions/setup-python | 5fda3b95a4ea91299a34e894583c3862153e4b97 | 7.0.0 |
| actions/upload-artifact | 043fb46d1a93c77aae656e7c1c64a875d1fc6a0a | 7.0.1 |
| github/codeql-action init + analyze | ff2f1c621b7f889edc0d3c761ac2e6a3f8cdb0dd | 4.37.7 |

已逐一读取上游对应 SHA 的 action.yml，均使用 node24 runtime。checkout 覆盖 ci/codeql/dependency-review/field-release；现有 Node 24.19.0、Python 3.12、cache 和产物配置保持原语义。CodeQL init/analyze 一起更新；不单独 cherry-pick analyze 旧 PR。最终 CI 还须确认无旧 Node20 action runtime deprecation warning。

vue-tsc 的直接版本仅 3.0.5→3.3.9；lockfile 的 Volar/Vue language tooling 等必要 dev 传递依赖随正常 npm 更新，生产直接依赖不变。要求 npm ci --ignore-scripts、lint、format:check、typecheck、test、build 全部 PASS。

本地上述六项已全部 PASS；unit 为 16 files / 89 tests。完整 CI 和两个真实容器 E2E 仍作为清理前硬门槛，以最终 HEAD 的 workflow 结果为准。

## Dependabot 治理与安全更新

保留 npm/pip/github-actions/docker 的 weekly 计划和原 PR limit。github-actions 的 version updates 统一一个 group；npm 的 minor+patch version updates 一个 group。两组都显式 applies-to: version-updates，不把安全更新混入版本更新的节奏。

仅为 pinia、typescript、vite、vitest 临时配置 update-types: version-update:semver-major；不配置整个包或版本范围 ignore，不 ignore axios，不关闭 security updates。S5 稳定后移除这四项 major ignore，再分别验收。

参考：[GitHub Dependabot 配置说明](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference)。[Dependabot ignore_condition 实现](https://github.com/dependabot/dependabot-core/blob/main/common/lib/dependabot/config/ignore_condition.rb) 在 security_updates_only 时直接返回显式 versions，不将本轮 semver update-types 转为安全更新忽略范围；本轮不配置 versions 忽略项。

## Fresh npm audit

升级前后均运行 npm audit --omit=dev --json 与 npm audit --json（通过既有 audit 脚本保留完整 JSON；生产 gate 使用 audit-level=critical）。本轮结果：

| 范围 | 升级前 Critical | 升级后 Critical | 升级前 High | 升级后 High |
|---|---|---|---|---|
| production | 0 | 0 | 6 | 6 |
| full | 2 | 2 | 17 | 17 |

完整 audit 另有 3 Moderate，均未因 vue-tsc 更新恶化。生产 Critical=0；既有开发依赖 Critical 和现存 High 的后续整改按上述延期验收处理，本轮不以 audit fix 扩张依赖变更。

## 清理前 PR / branch / head 台账

查询结果恰好为 dependabot[bot] 的 #1–#11。清理前必须再次核对集合与每个 head；集合不一致即停止删除并报告。以下为本轮首次审查保存的对照，删除不等于合并这些提交。

| PR | 精确分支 ref（不含 refs/heads/） | 审查时 head SHA |
|---|---|---|
| #1 | dependabot/github_actions/actions/setup-node-7.0.0 | 26a89f8dc455ac27c01b36b191d6efaa0f8333b1 |
| #2 | dependabot/github_actions/actions/upload-artifact-7.0.1 | 1683970dda979a7997d7505a20dc1c2c05ad8665 |
| #3 | dependabot/github_actions/actions/checkout-7.0.1 | 03b6b78166f33a6fd768df9155a5bc10392142a0 |
| #4 | dependabot/npm_and_yarn/apps/web/vitest-4.1.10 | cd2c03854ecbaf54a6a50192d61837a08db48ce3 |
| #5 | dependabot/npm_and_yarn/apps/web/typescript-7.0.2 | a919865694db66ec06f9d5c38718222f85c97f19 |
| #6 | dependabot/npm_and_yarn/apps/web/vite-8.2.1 | 2ec03d9eb81204a3a6f5320b42e6b092bf604ce1 |
| #7 | dependabot/npm_and_yarn/apps/web/axios-1.19.0 | 9120a7a09fb18da19087bf62577d1fe06eaa6f5d |
| #8 | dependabot/npm_and_yarn/apps/web/vue-tsc-3.3.9 | 379e61b18f44dc4bb798e5f1fd2d8c126a59640c |
| #9 | dependabot/npm_and_yarn/apps/web/pinia-4.0.2 | fc658598e2c1b677152c36c8040d6a476f0ce4d8 |
| #10 | dependabot/github_actions/actions/setup-python-7.0.0 | 4b1f66d8033d68ee8f3678370a6cb9aead0e17e7 |
| #11 | dependabot/github_actions/github/codeql-action/analyze-4.37.7 | bbcb82023ac88216aa100efa86a6d4014e74b083 |

已整合 PR #1/#2/#3/#8/#10/#11 的关闭说明：

> 已基于当前 feature HEAD 重新整合升级意图并通过完整 CI；未直接合并历史 Dependabot 分支，因此关闭旧 PR。

延期 PR #4/#5/#6/#7/#9 的关闭说明：

> 已完成工程审查；当前升级属于 major 或目标版本已过时，不进入 S5 前生产候选。决策已归档到 docs/开发验收/Dependabot依赖升级审查-20261022.md，关闭旧 PR 并清理历史分支。

清理只允许逐项对应上述 11 个 ref；保留 main、feature/vehicle-agent-hmi-v1、hardening/field-handoff-modular-deploy-v1 及任何不在台账中的分支。hardening 引用在本轮基线已不存在，按 S5A.2 保留要求从既有 baseline/server-runtime-2026-09-03 标签恢复到原 584efcfda60b8d39c516a9939bc0481609fc6f3c，不改提交历史。

gh 未认证时记录 GH_PR_CLOSE=BLOCKED_AUTH，不处理 token；仍可在 CI 全绿、集合正确后用已授权 git remote 清理上述精确分支。最终报告分别记录实际 PR 关闭数和远端剩余数，不把分支已删除冒充评论已发布或 API 关闭成功。

