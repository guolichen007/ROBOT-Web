# 测试与 Release Gate

本机统一执行：

```powershell
.\scripts\test.ps1
docker compose -f compose.test.yml --profile full up -d --build --wait
docker compose -f compose.test.yml --profile full down --volumes
.\scripts\build-ros2-handoff.ps1
```

门禁包含 backend、frontend、protocol、migration、containers、Playwright、security、CodeQL、文档链接、服务器暴露面、media auth、commit/event consistency、boot/target_boot、partition、backup/restore、server preflight、fault、soak、burst 和 clean clone。

故障测试不得通过删除断言或跳过测试制造 PASS。SERVER profile smoke 只证明配置在本机可启动，不等于第二台服务器已经部署。
