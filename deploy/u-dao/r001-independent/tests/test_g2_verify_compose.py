"""G2-R3 部署验证器回归：四侧（API / Dispatcher / r001-adapter / r001-ingress）G2 执行档一致。

1. 纯单元（CI 可跑）：用最小 compose config JSON 夹具直接调用 verify()。
2. 真实渲染（需 docker compose CLI，DOCKER_COMPOSE 指定；否则 skip）：对仓库 compose 文件
   执行 `compose config --format json` 后用验证器核验，并检查 8883/8884 与五服务镜像隔离。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SHA = "43d76f47332a949df386341642e9defdf0321f6e"
spec = importlib.util.spec_from_file_location("g2verify", HERE.parent / "g2-verify-compose.py")
g2verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g2verify)

G2_ENV = {
    "VEHICLE_AGENT_V1_DOWNLINK_ENABLED": "true",
    "VEHICLE_AGENT_V1_DOWNLINK_ALLOWLIST": "firebot-vehicle-01",
    "VEHICLE_TRANSPORT_PROFILES": "firebot-vehicle-01=r001-independent-v1",
    "TRANSPORT_PROFILE_ADAPTERS": "r001-independent-v1=r001-dedicated-adapter-v2",
    "VEHICLE_EXECUTION_PROFILES": "firebot-vehicle-01=ros-software-test",
}
PY = f"firebot-python:g2-{SHA}"


def fixture() -> tuple[dict, dict]:
    main = {
        "name": "u-dao-firebot",
        "services": {
            "api": {"image": PY, "environment": dict(G2_ENV)},
            "command-dispatcher": {
                "image": PY,
                "environment": dict(G2_ENV),
                "networks": {"firebot": {}, "r001": {}},
                "volumes": [
                    {"target": "/run/g2/dispatcher-broker-profiles.json"},
                    {"target": "/run/r001-secrets/r001-platform-pw"},
                ],
            },
            "web": {"image": f"firebot-web:g2-{SHA}"},
            "mosquitto": {
                "image": "eclipse-mosquitto:2.1.2-alpine",
                "ports": [{"published": "18883", "target": 8883}],
            },
            "vehicle-agent-adapter": {"image": "firebot-python:g1", "environment": {}},
        },
    }
    r001 = {
        "name": "u-dao-firebot-r001-prod",
        "services": {
            "r001-broker": {
                "image": "eclipse-mosquitto:2.1.2-alpine",
                "ports": [{"published": "8884", "target": 8883}],
            },
            "r001-adapter": {
                "image": PY,
                "environment": {
                    **G2_ENV,
                    "FIREBOT_SERVICE_INSTANCE_ID": "r001-dedicated-adapter-v2",
                    "VEHICLE_AGENT_TRANSPORT_PROFILE": "r001-independent-v1",
                },
            },
            "r001-ingress": {"image": PY, "environment": dict(G2_ENV)},
        },
    }
    return main, r001


def run(main, r001):
    return g2verify.verify(
        main, r001, SHA, "r001-dedicated-adapter-v2", copy.deepcopy(main), copy.deepcopy(r001)
    )


def test_four_sides_consistent_pass():
    assert run(*fixture()) == []


def test_ingress_profile_missing_fails():
    """反证：API profile=ros-software-test，Ingress profile=null（compose 未传入）→ 必须 FAIL。"""
    main, r001 = fixture()
    del r001["services"]["r001-ingress"]["environment"]["VEHICLE_EXECUTION_PROFILES"]
    problems = run(main, r001)
    assert any("VEHICLE_EXECUTION_PROFILES" in p and "r001-ingress" in p for p in problems), (
        problems
    )


@pytest.mark.parametrize("key", list(G2_ENV))
def test_any_ingress_g2_key_divergence_fails(key):
    main, r001 = fixture()
    r001["services"]["r001-ingress"]["environment"][key] = "drift"
    problems = run(main, r001)
    assert any(key in p and "r001-ingress" in p for p in problems), problems


def test_ingress_service_absent_fails():
    main, r001 = fixture()
    del r001["services"]["r001-ingress"]
    assert run(main, r001)


# ---------------------------------------------------------------- 真实 compose 渲染
COMPOSE = os.environ.get("DOCKER_COMPOSE") or shutil.which("docker-compose")


def render(tmp: Path, files: list[str], env_files: list[Path], project: str, profiles=()) -> dict:
    cmd = [COMPOSE, "-p", project]
    for profile in profiles:
        cmd += ["--profile", profile]
    for f in files:
        cmd += ["-f", str(ROOT / f)]
    for e in env_files:
        cmd += ["--env-file", str(e)]
    proc = subprocess.run(
        [*cmd, "config", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "SERVER_ENV_FILE": str(env_files[0])},
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    return json.loads(proc.stdout)


@pytest.fixture
def runtime(tmp_path):
    if not COMPOSE:
        pytest.skip("docker compose CLI 不可用（设置 DOCKER_COMPOSE）")
    conf = tmp_path / "r001conf"
    (conf / "certs").mkdir(parents=True)
    (conf / "certs" / "ca.crt").write_text("")
    (conf / "r001-platform-pw").write_text("")
    profiles = tmp_path / "dispatcher-broker-profiles.json"
    profiles.write_text("{}")
    server = tmp_path / "server.env"
    server.write_text((ROOT / ".env.server.example").read_text())
    g2 = tmp_path / "g2.env"
    g2.write_text(
        (HERE.parent / "g2.env.example")
        .read_text()
        .replace("<SHA>", SHA)
        .replace(
            "/srv/u-dao/projects/firebot/runtime/g2/dispatcher-broker-profiles.json", str(profiles)
        )
        + f"R001_BROKER_CONF_DIR={conf}\n"
    )
    r001 = tmp_path / "r001.env"
    r001.write_text(f"R001_BROKER_CONF_DIR={conf}\nR001_MQTT_PUBLIC_BIND=0.0.0.0:8884:8883\n")
    return {"server": server, "g2": g2, "r001": r001}


MAIN = ["docker-compose.server.yml", "deploy/u-dao/compose.u-dao.yml"]
OVERLAY = ["deploy/u-dao/r001-independent/compose.g2-dispatch.override.yml"]
R001 = ["deploy/u-dao/r001-independent/compose.r001-independent.yml"]


def test_real_render_four_side_pass_and_isolation(runtime):
    before = render(Path(), MAIN, [runtime["server"]], "u-dao-firebot", ("vehicle-agent",))
    main = render(
        Path(),
        MAIN + OVERLAY,
        [runtime["server"], runtime["g2"]],
        "u-dao-firebot",
        ("vehicle-agent",),
    )
    r001_before = render(Path(), R001, [runtime["r001"]], "u-dao-firebot-r001-prod")
    r001 = render(Path(), R001, [runtime["r001"], runtime["g2"]], "u-dao-firebot-r001-prod")
    ingress = r001["services"]["r001-ingress"]["environment"]
    assert ingress["VEHICLE_EXECUTION_PROFILES"] == "firebot-vehicle-01=ros-software-test"
    assert g2verify.verify(main, r001, SHA, "r001-dedicated-adapter-v2", before, r001_before) == []
    # 8883 / 8884 与 r001-broker 零变化
    assert r001["services"]["r001-broker"] == r001_before["services"]["r001-broker"]
    # 不带 g2.env（回滚/G1）时 ingress 五键为空缺省（fail-closed）
    g1_ingress = r001_before["services"]["r001-ingress"]["environment"]
    for key in G2_ENV:
        assert g1_ingress.get(key, "") in ("", "false"), (key, g1_ingress.get(key))
