from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
compose = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")
nginx = (ROOT / "infra/nginx/nginx.server.conf").read_text(encoding="utf-8")
env_example = (ROOT / ".env.server.example").read_text(encoding="utf-8")

assert 'ports: ["8883:8883"]' in compose
assert 'ports: ["${SERVER_HTTP_PORT:-80}:80", "${SERVER_HTTPS_PORT:-443}:443"]' in compose
assert compose.count("ports:") == 3, "unexpected additional published service"
for binding in (
    "${SERVER_TAILSCALE_IP:?SERVER_TAILSCALE_IP is required}:8554:8554/tcp",
    "${SERVER_LAN_IP:?SERVER_LAN_IP is required}:8189:8189/udp",
    "${SERVER_LAN_IP:?SERVER_LAN_IP is required}:8189:8189/tcp",
):
    assert f'"{binding}"' in compose, "media ports require explicit interface binding"
assert "9997:" not in compose and "8889:" not in compose
assert "0.0.0.0:8189" not in compose and '"8189:8189' not in compose
assert "location = /metrics" in nginx and "deny all" in nginx
assert "location = /health/ready" in nginx
assert "location ~ ^/api/(docs|openapi" in nginx
assert "server_tokens off" in nginx
assert "Strict-Transport-Security" in nginx
assert "Content-Security-Policy" in nginx
assert "Permissions-Policy" in nginx
assert "ENABLE_API_DOCS=false" in env_example
print("server surface OK: 80/443/8883; RTSP bound to Tailscale, WebRTC bound to LAN")
