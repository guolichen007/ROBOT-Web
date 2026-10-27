"""Static server-surface checks, per deployment mode.

legacy（默认）：旧现场服务器暴露面（80/443/8883 全接口；RTSP 绑 SERVER_TAILSCALE_IP、
WebRTC 绑 SERVER_LAN_IP；9997/8889 不映射）。注意：基座 compose 已把 :? 软化为 :-127.0.0.1
（compose 按文件解析先于 override 合并所致），legacy 缺变量 fail-closed 由
scripts/server-preflight.sh 恢复并由此处静态断言配合锁定。
u-dao：云部署（全部 127.0.0.1 高端口；Firebot 不占 80/443；无 0.0.0.0 绑定）。
合并后的权威校验见 scripts/test_compose_render.py（真实 docker compose config 渲染）。
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--mode", choices=["legacy", "u-dao"], default="legacy")
args = parser.parse_args()


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def check_legacy() -> None:
    compose = read("docker-compose.server.yml")
    nginx = read("infra/nginx/nginx.server.conf")
    env_example = read(".env.server.example")

    assert 'ports: ["8883:8883"]' in compose
    assert 'ports: ["${SERVER_HTTP_PORT:-80}:80", "${SERVER_HTTPS_PORT:-443}:443"]' in compose
    assert compose.count("ports:") == 3, "unexpected additional published service"
    for binding in (
        "${SERVER_TAILSCALE_IP:-127.0.0.1}:8554:8554/tcp",
        "${SERVER_LAN_IP:-127.0.0.1}:8189:8189/udp",
        "${SERVER_LAN_IP:-127.0.0.1}:8189:8189/tcp",
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
    print("legacy server surface OK: 80/443/8883; RTSP bound to SERVER_TAILSCALE_IP, WebRTC bound to SERVER_LAN_IP")


def check_u_dao() -> None:
    override = read("deploy/u-dao/compose.u-dao.yml")
    edge = read("infra/nginx/nginx.edge.http.conf")
    mediamtx = read("infra/mediamtx/mediamtx.server.yml")

    expected = (
        '"127.0.0.1:18883:8883"',
        '"127.0.0.1:18554:8554/tcp"',
        '"127.0.0.1:18890:8890/udp"',
        '"127.0.0.1:8189:8189/udp"',
        '"127.0.0.1:8189:8189/tcp"',
        '"127.0.0.1:18080:80"',
    )
    for binding in expected:
        assert binding in override, f"missing cloud binding {binding}"
    assert override.count("ports: !override") == 3, "cloud override must !override ports of mosquitto/mediamtx/nginx"
    # 宿主绑定不得为全接口（"0.0.0.0" 仅允许出现在容器内监听，如 api --host 0.0.0.0）
    # 权威校验：test_compose_render.py 对渲染后 host_ip 的断言。
    for forbidden in (":5432", ":6379", ":9997", ":8889", ":1883", ":443"):
        assert forbidden not in override, f"forbidden exposure {forbidden}"
    assert "srt: yes" in mediamtx and "srtAddress: :8890" in mediamtx
    assert "authMethod: http" in mediamtx and "srtPublishPassphrase:" in mediamtx
    assert "X-Forwarded-Proto $http_x_forwarded_proto" in edge
    assert "proxy_set_header Authorization $http_authorization" in edge
    assert "server_tokens off" in edge
    assert "location ~ ^/api/(docs|openapi\\.json) { return 404; }" in edge
    print("u-dao server surface OK: loopback-only high ports; no 80/443; SRT listener enabled; edge forwards XFP/Authorization")


if args.mode == "legacy":
    check_legacy()
else:
    check_u_dao()
sys.exit(0)
