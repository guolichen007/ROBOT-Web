# U-Dao Shared Ingress（platform 资源）实例化说明

## 所有权与边界

- shared ingress 是 **U-Dao platform 资源**，canonical 位置 `/srv/u-dao/platform/ingress`，
  独立 compose project `u-dao-ingress`，不属于任何业务项目（Firebot）的生命周期。
- **Firebot 的部署/升级/回滚脚本禁止对 u-dao-ingress project 执行 down/up/restart。**
- 本目录只提供 Firebot 的路由片段模板 `conf.d/firebot.conf.template`；
  平台主配置（nginx.conf、compose 文件、TLS 证书）由 platform 维护，本仓库不提交真值。

## 实例化步骤（服务器侧，platform 维护者执行）

```bash
mkdir -p /srv/u-dao/platform/ingress/{conf.d,tls}
cp conf.d/firebot.conf.template /srv/u-dao/platform/ingress/conf.d/firebot.conf
# TLS：bootstrap 证书由 deploy/u-dao/scripts/02-gen-bootstrap-secrets.sh 生成至
# /srv/u-dao/platform/ingress/tls/；正式公网证书由 OWNER 后续替换（OWNER_ACTION_REQUIRED）。
```

主配置 `/srv/u-dao/platform/ingress/nginx.conf` 采用：

```nginx
worker_processes auto;
events { worker_connections 2048; }
http {
  include /etc/nginx/conf.d/*.conf;   # 挂载 conf.d/（各项目片段；crane-platform 后续自加片段）
}
```

compose（project `u-dao-ingress`）：

```yaml
name: u-dao-ingress
services:
  nginx:
    image: nginx:1.30.4-alpine
    restart: unless-stopped
    ports:
      - "127.0.0.1:18000:18000"
      - "127.0.0.1:18443:18443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./conf.d:/etc/nginx/conf.d:ro
      - ./tls:/etc/nginx/tls:ro
    networks: [u-dao-ingress]
networks:
  u-dao-ingress:
    name: u-dao-ingress
```

## edge 重建后的 DNS 恢复

- upstream `server firebot-edge:80 resolve` + `zone`：firebot edge 容器 recreate 后，
  nginx 按 Docker DNS TTL 自动重新解析，无需人工 reload（故障注入验收覆盖此场景）。
- 兜底安全 reload（必要时）：`docker compose -p u-dao-ingress exec nginx nginx -s reload`
  —— 该操作属于 platform 维护，Firebot 脚本不得代执行。

## 公网规划（OWNER_ACTION_REQUIRED）

- 生产 80/443：由 platform 主配置监听 + 正式域名/TLS；本片段届时保持不动（server_name 匹配）。
- 多项目：crane-platform 等后续项目在 conf.d/ 增加自己的片段，互不干扰。
