# Ubuntu Server Deployment

Target: Ubuntu Server with Docker Engine and Compose v2. Windows is supported for development, not the preferred 24×7 host.

## Preparation

1. Clone the repository and check out the signed-off `main` commit.
2. Copy `.env.server.example` to an untracked `.env.server`; replace every placeholder using a secret manager or root-readable secret files.
3. Install TLS certificate/key under an operator-controlled path and Mosquitto password/ACL files with least privilege.
4. Create persistent backup storage outside the repository. Confirm restore ownership and retention.
5. Run `docker compose --env-file .env.server -f docker-compose.server.yml config` and review every mount and published port.

Recommended firewall exposure: 80/443 for reverse proxy, 8883 for MQTT TLS, optional 3478 TCP/UDP and 49160-49200 UDP for TURN, and operator-only SSH/VPN. Do not publish PostgreSQL, Redis, MediaMTX management API or ROS2 DDS.

Start with:

```bash
docker compose --env-file .env.server -f docker-compose.server.yml pull
docker compose --env-file .env.server -f docker-compose.server.yml up -d
docker compose --env-file .env.server -f docker-compose.server.yml ps
curl --fail https://YOUR_HOST/health/ready
```

The server profile disables Mock/demo/anonymous MQTT, uses persistent volumes, restart policies, health checks and log rotation. Add a systemd unit invoking the exact Compose file and `Requires=docker.service`; do not embed secrets in the unit or Docker logs.

`SERVER_DEPLOYMENT_READY` means the package is prepared and validated locally. It does not mean a second machine, public DNS, CA-issued TLS, site ACL or real robot has been deployed.
