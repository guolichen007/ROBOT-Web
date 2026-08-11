# 贡献指南

1. 从最新 `develop` 创建范围清晰的分支。
2. 不提交 `.env`、secret、证书、备份、真实地图/媒体或个人数据。
3. 修改协议时先改 canonical Schema，并同步 generated models、Mock、tester、handoff。
4. 本机执行 `.\scripts\test.ps1`、handoff build 和相关 full-stack/fault 测试。
5. PR 描述必须写清变更、原因、风险、迁移和测试证据。
6. required checks 全绿后才合入 develop；发布 PR 再由 develop 合入 main。

禁止 force push/delete main/develop、删除失败测试、伪造真实 ROS2/服务器/视频 PASS。安全问题不要公开 Issue，按 [SECURITY.md](SECURITY.md) 私下报告。
