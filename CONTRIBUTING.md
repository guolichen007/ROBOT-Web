# 贡献指南

## 分支策略

```text
main
  ↑ PR
feature/*  fix/*  maintenance/*  field/*  release/*
```

1. 从最新 `main` 创建范围清晰的分支（`feature/…`、`fix/…`、`maintenance/…`、`field/…`、`release/…`）。
2. 功能/修复/维护/现场/发布分支通过 PR 合入 `main`，合入后删除临时分支。
3. 不提交 `.env`、secret、证书、备份、真实地图/媒体或个人数据。
4. 修改协议时先改 canonical Schema，并同步 generated models、Mock、tester、handoff。
5. 本机执行 `.\scripts\test.ps1`、handoff build 和相关 full-stack/fault 测试。
6. PR 描述必须写清变更、原因、风险、迁移和测试证据。
7. required checks 全绿后才合入 `main`；正式 Release 只从 `main` 打 tag。

长期分支只保留 `main`。历史 `develop`、`integration/**`、`hardening/**` 已收敛进 `main`，历史版本由 tag 保存（如 `baseline/server-runtime-2026-09-03`），不再作为开发流程分支使用。

## 现场发布（Field Release）

现场模块验收使用独立 `FIELD_RELEASE_GATE`（`workflow_dispatch` + `field/**`、`release/**` 触发），不因已知的历史业务测试问题而整体 skipped。

## 禁止

- force push / delete `main`。
- 删除失败测试。
- 伪造真实 ROS2 / 服务器 / 视频 / 实车验收 PASS。
- 安全问题不要公开 Issue，按 [SECURITY.md](SECURITY.md) 私下报告。
