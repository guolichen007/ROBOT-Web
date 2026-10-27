#!/usr/bin/env python3
"""PUBLIC 媒体端口绑定启动级回归（P0-A，配合 test_compose_render.py 的结构断言）。

背景：8189 双绑定事故（公网激活实测）证明 `docker compose config` 只能证明 YAML 合并
正确，不能证明 Docker 能实际 bind——compose.u-dao.yml 的 loopback 8189 与公网层
0.0.0.0:8189 同宿主端口双绑定，render 通过但启动 EADDRINUSE。
本测试用已构建的 mediamtx 镜像真实启动容器，并锁定负例（防回归）。

覆盖：
  LOCAL profile mediamtx starts PASS（base+cloud，全 loopback）
  PUBLIC profile mediamtx starts PASS（base+cloud+public，!override 完整端口集）
  PUBLIC profile UDP 8189 PASS / TCP 8189 PASS / SRT 8890 PASS（docker port 运行时映射）
  无 address already in use（双绑定负例 docker run 必须失败）
  无 duplicate host binding（结构断言 + 负例）

用法（CI containers job 内，mediamtx 镜像已构建）：
  python3 scripts/test_public_bind.py
本地/端口被占用时可用 --port-offset 平移宿主端口（拓扑不变）：
  python3 scripts/test_public_bind.py --port-offset 30000 --no-startup   # 仅结构断言
env:
  PUBLIC_BIND_MEDIAMTX_IMAGE  默认 firebot-test-mediamtx:ci
  PUBLIC_BIND_MEDIAMTX_CONF   默认 infra/mediamtx/mediamtx.server.yml（挂 /mediamtx.yml）
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} {detail}")


def docker() -> list[str]:
    for candidate in (["docker"], ["sudo", "-n", "docker"]):
        try:
            if (
                subprocess.run(candidate + ["info"], capture_output=True, timeout=60).returncode
                == 0
            ):
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise RuntimeError("docker 与 sudo -n docker 均不可用")


def compose_config_cmd() -> list[str]:
    """compose config 为纯客户端渲染（无需 daemon）：优先非 sudo——
    sudo 的 env_reset 会剥掉公网绑定插值变量（与部署脚本同款陷阱）。"""
    return ["docker", "compose"]


def render_ports(files: list[str], extra_env: dict | None = None) -> dict[str, list[dict]]:
    """渲染 compose config JSON，返回 {service: [port dict]}。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env.server"
        env.write_text(
            "APP_ENV=server\nENABLE_API_DOCS=false\n"
            f"SERVER_ENV_FILE={env}\n"
            "MEDIAMTX_WEBRTC_ALLOW_ORIGINS=https://firebot.example\n"
            "MEDIAMTX_WEBRTC_ADDITIONAL_HOSTS=firebot.example\n"
            "POSTGRES_DB=firebot\nPOSTGRES_USER=firebot\n",
            encoding="utf-8",
        )
        cmd = compose_config_cmd() + ["--env-file", str(env)]
        for f in files:
            cmd += ["-f", str(ROOT / f)]
        cmd += ["config", "--format", "json"]
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, **(extra_env or {})},
        )
        if proc.returncode != 0:
            raise RuntimeError(f"compose config 失败:\n{proc.stderr[-500:]}")
        cfg = json.loads(proc.stdout)
        return {svc: spec.get("ports", []) for svc, spec in cfg["services"].items()}


def port_flag(p: dict, offset: int) -> str:
    host_ip = p.get("host_ip") or "0.0.0.0"
    published = int(p["published"]) + offset
    target = p["target"]
    proto = p.get("protocol", "tcp")
    return f"{host_ip}:{published}:{target}/{proto}"


def start_container(name: str, flags: list[str], image: str, conf: Path) -> str:
    # 注意：docker run 的 -p 必须在镜像名之前（镜像 entrypoint 会把其后参数当自身参数）
    d = docker()
    run = d + ["run", "-d", "--name", name]
    for f in flags:
        run += ["-p", f]
    run += ["-v", f"{conf}:/mediamtx.yml:ro", image]
    proc = subprocess.run(run, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def docker_port(cid: str) -> str:
    proc = subprocess.run(docker() + ["port", cid], capture_output=True, text=True, timeout=60)
    return proc.stdout


def container_logs(cid: str) -> str:
    proc = subprocess.run(docker() + ["logs", cid], capture_output=True, text=True, timeout=60)
    return proc.stdout + proc.stderr


def rm_container(name: str) -> None:
    subprocess.run(
        docker() + ["rm", "-f", name],
        capture_output=True,
        text=True,
        timeout=60,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-startup", action="store_true", help="只做渲染结构断言")
    parser.add_argument(
        "--port-offset", type=int, default=0, help="宿主端口平移量（本地/端口占用时）"
    )
    args = parser.parse_args()
    image = os.environ.get("PUBLIC_BIND_MEDIAMTX_IMAGE", "firebot-test-mediamtx:ci")
    conf = ROOT / os.environ.get("PUBLIC_BIND_MEDIAMTX_CONF", "infra/mediamtx/mediamtx.server.yml")
    if not conf.exists():
        print(f"FAIL mediamtx 配置不存在 {conf}")
        return 1
    offset = args.port_offset

    # ---- 1) 结构断言（LOCAL / PUBLIC 渲染）----
    local = render_ports(["docker-compose.server.yml", "deploy/u-dao/compose.u-dao.yml"])
    mtx_local = local["mediamtx"]
    expected_local = {
        ("127.0.0.1", "18554", "8554", "tcp"),
        ("127.0.0.1", "18890", "8890", "udp"),
        ("127.0.0.1", "8189", "8189", "udp"),
        ("127.0.0.1", "8189", "8189", "tcp"),
    }
    actual_local = {
        (str(p.get("host_ip")), str(p.get("published")), str(p.get("target")), p.get("protocol"))
        for p in mtx_local
    }
    check(
        "LOCAL profile mediamtx 端口集精确（全 loopback，含 8189 端口保持）",
        actual_local == expected_local,
        f"diff={actual_local ^ expected_local}",
    )

    public = render_ports(
        [
            "docker-compose.server.yml",
            "deploy/u-dao/compose.u-dao.yml",
            "deploy/u-dao/compose.u-dao-public.yml",
        ],
        {
            "MQTT_PUBLIC_BIND": "0.0.0.0:8883:8883",
            "SRT_PUBLIC_BIND": "0.0.0.0:8890:8890/udp",
            "WEBRTC_PUBLIC_BIND_UDP": "0.0.0.0:8189:8189/udp",
            "WEBRTC_PUBLIC_BIND_TCP": "0.0.0.0:8189:8189/tcp",
        },
    )
    mtx_public = public["mediamtx"]
    expected_public = {
        ("127.0.0.1", "18554", "8554", "tcp"),
        ("127.0.0.1", "18890", "8890", "udp"),
        ("0.0.0.0", "8890", "8890", "udp"),
        ("0.0.0.0", "8189", "8189", "udp"),
        ("0.0.0.0", "8189", "8189", "tcp"),
    }
    actual_public = {
        (str(p.get("host_ip")), str(p.get("published")), str(p.get("target")), p.get("protocol"))
        for p in mtx_public
    }
    check(
        "PUBLIC profile mediamtx 端口集精确（!override，无 loopback 8189）",
        actual_public == expected_public,
        f"diff={actual_public ^ expected_public}",
    )

    duplicate = []
    for svc, ports in public.items():
        seen: dict = {}
        for p in ports:
            key = (p.get("published"), p.get("protocol"))
            seen.setdefault(key, set()).add(p.get("host_ip"))
        for key, ips in seen.items():
            if "127.0.0.1" in ips and "0.0.0.0" in ips:
                duplicate.append((svc, key))
    check("无 duplicate host binding（结构）", not duplicate, str(duplicate))

    # ---- 2) 启动级（真实 docker run + docker port 运行时映射）----
    if args.no_startup:
        print("SKIP startup（--no-startup）")
    else:
        try:
            # LOCAL profile 实际启动
            rc, cid, err = start_container(
                "pub-bind-local", [port_flag(p, offset) for p in mtx_local], image, conf
            )
            check("LOCAL profile mediamtx starts PASS", rc == 0, err[-300:])
            if rc == 0:
                time.sleep(4)
                ports_out = docker_port(cid)
                for p in mtx_local:
                    pub = str(int(p["published"]) + offset)
                    proto = p.get("protocol", "tcp")
                    check(
                        f"LOCAL {p['target']}/{proto} → {p.get('host_ip')}:{pub}",
                        f"{p['target']}/{proto} -> {p.get('host_ip')}:{pub}" in ports_out,
                        ports_out[-200:],
                    )
                logs = container_logs(cid)
                check(
                    "LOCAL mediamtx SRT 8890 listener 打开",
                    "[SRT] listener opened on :8890" in logs,
                    logs[-300:],
                )
                check(
                    "LOCAL mediamtx WebRTC 8189 listener 打开",
                    ":8189 (UDP/ICE)" in logs and ":8189 (TCP/ICE)" in logs,
                    logs[-300:],
                )
            rm_container("pub-bind-local")

            # PUBLIC profile 实际启动
            rc, cid, err = start_container(
                "pub-bind-public", [port_flag(p, offset) for p in mtx_public], image, conf
            )
            check("PUBLIC profile mediamtx starts PASS", rc == 0, err[-300:])
            if rc == 0:
                time.sleep(4)
                ports_out = docker_port(cid)
                check(
                    "PUBLIC SRT 8890/udp 运行时绑定",
                    f"8890/udp -> 0.0.0.0:{8890 + offset}" in ports_out,
                    ports_out,
                )
                check(
                    "PUBLIC UDP 8189 运行时绑定",
                    f"8189/udp -> 0.0.0.0:{8189 + offset}" in ports_out,
                    ports_out,
                )
                check(
                    "PUBLIC TCP 8189 运行时绑定",
                    f"8189/tcp -> 0.0.0.0:{8189 + offset}" in ports_out,
                    ports_out,
                )
                logs = container_logs(cid)
                check(
                    "PUBLIC mediamtx SRT listener 打开",
                    "[SRT] listener opened on :8890" in logs,
                    logs[-300:],
                )
                check(
                    "PUBLIC mediamtx WebRTC 8189 listener 打开",
                    ":8189 (UDP/ICE)" in logs and ":8189 (TCP/ICE)" in logs,
                    logs[-300:],
                )

                # 负例：双绑定（回归护栏——旧 bug 的组合必须启动失败）
                dual = [
                    port_flag(p, offset)
                    for p in [
                        {
                            "host_ip": "127.0.0.1",
                            "published": "8189",
                            "target": "8189",
                            "protocol": "tcp",
                        },
                        {
                            "host_ip": "0.0.0.0",
                            "published": "8189",
                            "target": "8189",
                            "protocol": "tcp",
                        },
                    ]
                ]
                rc2, _, err2 = start_container("pub-bind-dual-neg", dual, image, conf)
                check("负例：127.0.0.1:8189 + 0.0.0.0:8189 双绑定必须失败", rc2 != 0, f"rc={rc2}")
                rm_container("pub-bind-dual-neg")

                # 负例：重复绑定已占用端口必须失败（证明绑定真实生效）
                rc3, _, _ = start_container(
                    "pub-bind-occupied-neg",
                    [f"0.0.0.0:{8189 + offset}:8189/udp"],
                    image,
                    conf,
                )
                check("负例：占用中的 8189/udp 再次绑定必须失败", rc3 != 0, f"rc={rc3}")
                rm_container("pub-bind-occupied-neg")
            rm_container("pub-bind-public")
        finally:
            for name in (
                "pub-bind-local",
                "pub-bind-public",
                "pub-bind-dual-neg",
                "pub-bind-occupied-neg",
            ):
                rm_container(name)

    if FAILURES:
        print(f"\nPUBLIC_BIND_TEST=FAIL count={len(FAILURES)}")
        return 1
    print("\nPUBLIC_BIND_TEST=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
