# Windows 干净环境启动

1. 安装 Git for Windows、Docker Desktop，启用 WSL2 backend。
2. 确认 `git --version`、`docker version`、`docker compose version` 正常。
3. 使用 SSH clone：

```powershell
git clone git@github.com:guolichen007/ROBOT-Web.git C:\Users\13576\Desktop\web_robot
Set-Location C:\Users\13576\Desktop\web_robot
Copy-Item .env.example .env
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\dev.ps1
```

Web 为 `http://localhost:8080`。首次启动终端显示一次性 admin 密码，首次登录必须修改。运行测试用 `.\scripts\test.ps1`，停止用 `.\scripts\stop.ps1`。

若 8080/1883/8554/8889/9997 被占用，先用 `Get-NetTCPConnection -State Listen` 定位；不要修改 SERVER 公网暴露规则来规避端口冲突。Windows 防火墙只需允许本机开发所需端口，不应将 DEV Broker 暴露到不可信网络。

更新代码：停止 DEV、`git pull --ff-only`、重新执行 `dev.ps1`。不要提交 `.env`、`backups/`、`dist/`、证书或现场文件。
