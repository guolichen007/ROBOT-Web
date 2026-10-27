#!/usr/bin/env python3
"""Compose render tests: legacy surface regression lock + U-Dao cloud override assertions.

Runs real `docker compose config` against synthetic env files (no server secrets involved).
Also unit-tests the restored legacy fail-closed semantics of scripts/server-preflight.sh.
Usage: python3 scripts/test_compose_render.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = shutil.which("docker")
assert COMPOSE, "docker CLI not found"

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def compose_config(env_file: Path, files: list[str]) -> dict:
    cmd = [COMPOSE, "compose", "--env-file", str(env_file)]
    for f in files:
        cmd += ["-f", str(ROOT / f)]
    cmd += ["config", "--format", "json"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"compose config failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def ports_of(cfg: dict, svc: str) -> list[dict]:
    return cfg["services"][svc].get("ports", [])


def has_port(ports: list[dict], published, protocol: str, host_ip=None) -> bool:
    for p in ports:
        if str(p.get("published")) == str(published) and p.get("protocol") == protocol:
            if host_ip is None:
                return True
            if p.get("host_ip") == host_ip:
                return True
    return False


def test_legacy_render() -> None:
    """旧现场表面回归锁：渲染结果必须与历史行为一致（RTSP 绑 Tailscale、WebRTC 绑 LAN、80/443/8883）。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env.server"
        env.write_text(
            "APP_ENV=server\nENABLE_API_DOCS=false\n"
            f"SERVER_ENV_FILE={env}\n"
            "SERVER_TAILSCALE_IP=100.64.0.2\nSERVER_LAN_IP=192.168.110.101\n"
            "MEDIAMTX_WEBRTC_ALLOW_ORIGINS=https://firebot.example\n"
            "MEDIAMTX_WEBRTC_ADDITIONAL_HOSTS=192.168.110.101\n"
            "POSTGRES_DB=firebot\nPOSTGRES_USER=firebot\n",
            encoding="utf-8",
        )
        cfg = compose_config(env, ["docker-compose.server.yml"])
        check("legacy name=firebot-server", cfg.get("name") == "firebot-server", str(cfg.get("name")))
        check("legacy mosquitto 8883:8883", has_port(ports_of(cfg, "mosquitto"), 8883, "tcp"))
        check("legacy nginx 80", has_port(ports_of(cfg, "nginx"), 80, "tcp"))
        check("legacy nginx 443", has_port(ports_of(cfg, "nginx"), 443, "tcp"))
        check("legacy mediamtx 8554 bound to Tailscale IP",
              has_port(ports_of(cfg, "mediamtx"), 8554, "tcp", "100.64.0.2"))
        check("legacy mediamtx 8189/udp bound to LAN IP",
              has_port(ports_of(cfg, "mediamtx"), 8189, "udp", "192.168.110.101"))
        check("legacy mediamtx 8189/tcp bound to LAN IP",
              has_port(ports_of(cfg, "mediamtx"), 8189, "tcp", "192.168.110.101"))
        check("legacy no 9997/8889 exposure",
              not has_port(ports_of(cfg, "mediamtx"), 9997, "tcp")
              and not has_port(ports_of(cfg, "mediamtx"), 8889, "tcp"))


RUNTIME = "/srv/u-dao/projects/firebot/runtime"


def test_u_dao_render() -> None:
    """U-Dao 云模式渲染：全部 127.0.0.1 高端口、无 443、secrets 绝对路径、edge 别名入 ingress 网络。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env.server"
        # 云 env 刻意不定义 SERVER_TAILSCALE_IP/SERVER_LAN_IP（基座必须能无它们渲染）
        env.write_text(
            "APP_ENV=server\nENABLE_API_DOCS=false\n"
            f"SERVER_ENV_FILE={env}\n"
            "MEDIAMTX_WEBRTC_ALLOW_ORIGINS=https://firebot.local:18443\n"
            "MEDIAMTX_WEBRTC_ADDITIONAL_HOSTS=127.0.0.1,firebot.local,u-dao\n"
            "POSTGRES_DB=firebot\nPOSTGRES_USER=firebot\n",
            encoding="utf-8",
        )
        cfg = compose_config(env, ["docker-compose.server.yml", "deploy/u-dao/compose.u-dao.yml"])
        check("u-dao name=u-dao-firebot", cfg.get("name") == "u-dao-firebot", str(cfg.get("name")))
        check("u-dao mosquitto 127.0.0.1:18883:8883",
              has_port(ports_of(cfg, "mosquitto"), 18883, "tcp", "127.0.0.1"))
        check("u-dao mediamtx 127.0.0.1:18554:8554",
              has_port(ports_of(cfg, "mediamtx"), 18554, "tcp", "127.0.0.1"))
        check("u-dao mediamtx 127.0.0.1:18890:8890/udp (SRT)",
              has_port(ports_of(cfg, "mediamtx"), 18890, "udp", "127.0.0.1"))
        check("u-dao mediamtx 127.0.0.1:8189 udp+tcp（端口保持）",
              has_port(ports_of(cfg, "mediamtx"), 8189, "udp", "127.0.0.1")
              and has_port(ports_of(cfg, "mediamtx"), 8189, "tcp", "127.0.0.1"))
        check("u-dao nginx 127.0.0.1:18080:80", has_port(ports_of(cfg, "nginx"), 18080, "tcp", "127.0.0.1"))
        check("u-dao nginx 无 443", not has_port(ports_of(cfg, "nginx"), 443, "tcp"))
        check("u-dao 无 0.0.0.0 绑定", all(p.get("host_ip", "0.0.0.0") != "0.0.0.0"
                                        for svc in cfg["services"] for p in ports_of(cfg, svc)))
        api_cmd = cfg["services"]["api"].get("command")
        check("u-dao api --proxy-headers", api_cmd and "--proxy-headers" in api_cmd, str(api_cmd))
        nginx_nets = cfg["services"]["nginx"].get("networks", {})
        check("u-dao edge 别名 firebot-edge 入 u-dao-ingress 网络",
              "u-dao-ingress" in nginx_nets
              and "firebot-edge" in (nginx_nets["u-dao-ingress"] or {}).get("aliases", []))
        top_nets = cfg.get("networks", {})
        check("u-dao 顶层 external u-dao-ingress",
              top_nets.get("u-dao-ingress", {}).get("external") is True
              and top_nets["u-dao-ingress"].get("name") == "u-dao-ingress")
        for name in ("database_url", "redis_url", "jwt_secret", "mqtt_platform_password",
                     "media_publish_token", "mqtt_ca"):
            check(f"u-dao secret {name} 指向 runtime 绝对路径",
                  name in cfg.get("secrets", {}), f"missing secret {name}")
        # srt_publish_passphrase 不经过 compose secret：由 runtime 渲染的
        # mediamtx.server.u-dao.yml（per-path srtPublishPassphrase）注入，见 mediamtx 卷断言。
        # mediamtx 挂载运行时渲染配置
        mtx_vols = {v.get("target"): v.get("source") for v in cfg["services"]["mediamtx"].get("volumes", [])}
        check("u-dao mediamtx 配置指向 runtime 渲染 yml",
              mtx_vols.get("/mediamtx.yml") == f"{RUNTIME}/env/mediamtx.server.u-dao.yml",
              str(mtx_vols.get("/mediamtx.yml")))


def docker_directly_accessible() -> bool:
    return subprocess.run([COMPOSE, "info"], capture_output=True, timeout=60).returncode == 0


def run_preflight(env_file: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """preflight 需要 docker daemon；无 docker group 权限时用 sudo -n env 包装（CI 直连分支不变）。"""
    base_env = dict(extra_env or {})
    base_env.setdefault("U_DAO_MODE", "0")
    base_env.update({"SERVER_ENV_FILE": str(env_file), "BACKUP_PATH": str(env_file.parent / "backups")})
    if docker_directly_accessible():
        env = {**os.environ, **base_env}
        return subprocess.run(["bash", "scripts/server-preflight.sh"], cwd=ROOT,
                              capture_output=True, text=True, timeout=180, env=env)
    cmd = ["sudo", "-n", "env"] + [f"{k}={v}" for k, v in base_env.items()] + \
        ["bash", "scripts/server-preflight.sh"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=180)
    # 清理 root 产生的文件，保证 TemporaryDirectory 可回收
    subprocess.run(["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(env_file.parent)],
                   capture_output=True, timeout=60)
    return proc


def test_legacy_preflight_fail_closed() -> None:
    """R2-12：legacy 模式缺 SERVER_TAILSCALE_IP/SERVER_LAN_IP 必须 FAIL（基座 :? 软化后的语义恢复）。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env.server"
        env.write_text("APP_ENV=server\nENABLE_API_DOCS=false\n", encoding="utf-8")
        proc = run_preflight(env)
        check("legacy preflight 缺变量 → FAIL", proc.returncode != 0)
        check("legacy preflight 报 SERVER_TAILSCALE_IP 语义",
              "SERVER_TAILSCALE_IP" in (proc.stdout + proc.stderr))
        env2 = Path(tmp) / ".env2.server"
        env2.write_text(
            "APP_ENV=server\nENABLE_API_DOCS=false\nSERVER_TAILSCALE_IP=192.0.2.11\nSERVER_LAN_IP=192.0.2.10\n",
            encoding="utf-8",
        )
        proc2 = run_preflight(env2)
        check("legacy preflight 占位 IP → FAIL", proc2.returncode != 0)
        # U-Dao 模式定义 Tailscale 变量 → FAIL
        proc3 = run_preflight(env2, {"U_DAO_MODE": "1", "FIREBOT_RUNTIME_ROOT": str(tmp), "FIREBOT_COMPOSE_FILES": "docker-compose.server.yml deploy/u-dao/compose.u-dao.yml"})
        check("u-dao preflight 定义 SERVER_TAILSCALE_IP → FAIL", proc3.returncode != 0)


def main() -> int:
    test_legacy_render()
    test_u_dao_render()
    test_legacy_preflight_fail_closed()
    if FAILURES:
        print(f"\nCOMPOSE_RENDER_TEST=FAIL count={len(FAILURES)}")
        return 1
    print("\nCOMPOSE_RENDER_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
