# Firebot Cloud Control Platform

智能灭火机器人云控平台的正式 V2 Baseline 仓库。

本仓库负责 Web、FastAPI、PostgreSQL、Redis、MQTT、WebSocket、媒体接入框架、权限审计、Mock Robot、测试与部署包；不包含 ROS2、SLAM、Nav2、传感器驱动、底盘、灭火执行机构或车端安全 watchdog。

当前唯一实施规格：`CODEX_MASTER_SPEC_FIREBOT_V2_BASELINE_FINAL.md`。

开发分支：`develop`；通过全部本机验收后才合并 `main`。

## Windows 本机运行

前置条件：Docker Desktop / Docker Engine 与 Git。首次启动：

```powershell
Set-Location C:\Users\13576\Desktop\web_robot
.\scripts\dev.ps1
```

默认统一入口为 `http://localhost:8080`，API 文档为 `http://localhost:8080/api/docs`，健康检查为 `http://localhost:8080/health/ready`。开发管理员密码由首次运行脚本生成并只显示一次；仓库不保存实际 `.env` 或密码。

停止与测试：

```powershell
.\scripts\stop.ps1
.\scripts\test.ps1
```

## Profiles

- `compose.dev.yml`：本机开发，幂等 seed 和真实 MQTT Mock R001。
- `compose.test.yml`：独立临时数据库、Redis 和 Mosquitto。
- `docker-compose.server.yml`：服务器部署就绪配置；默认关闭 Mock、匿名 MQTT 和 demo seed。

第二台服务器尚未部署。当前状态是 `SERVER_DEPLOYMENT_READY`，不是 `SERVER_DEPLOYED`。
