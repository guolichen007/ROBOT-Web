# 平台安全设计

认证使用 Argon2id、短期 access token、HttpOnly refresh cookie、rotation/revoke、CSRF、登录限流和 WebSocket 一次性 ticket。权限按 manual/stop/e-stop/reset/task/force-release 拆分。

SERVER secret 优先 Docker secrets/`*_FILE`，`.env.server` 只保存非敏感配置和 secret path。preflight 遇到缺失、空值、placeholder、弱媒体 token、匿名 MQTT、Mock/demo、非 TLS 或 API docs 开启时 fail-fast。

公网仅 80/443/8883；ready、metrics、MediaMTX admin、PostgreSQL、Redis 不公网。Nginx 启用 HSTS、nosniff、Referrer-Policy、CSP/frame-ancestors、Permissions-Policy、server_tokens off。

浏览器 media ticket 绑定 user/robot/camera/expiry；MediaMTX HTTP auth 对 read/publish 回调平台。报告漏洞请遵循根目录 [SECURITY.md](../SECURITY.md)。
