#!/usr/bin/env python3
"""G2 生产 compose 展开核验（只读：只消费 `docker compose config --format json` 输出）。

用法（生产主机，发布前；不启动/不重建任何容器）：
  docker compose -p u-dao-firebot -f docker-compose.server.yml -f deploy/u-dao/compose.u-dao.yml \\
    -f deploy/u-dao/r001-independent/compose.g2-dispatch.override.yml \\
    --env-file <runtime>/server.env --env-file <runtime>/g2.env config --format json > main.json
  docker compose -f deploy/u-dao/r001-independent/compose.r001-independent.yml \\
    --env-file <runtime>/r001.env --env-file <runtime>/g2.env config --format json > r001.json
  python3 deploy/u-dao/r001-independent/g2-verify-compose.py main.json r001.json --sha <G2_SHA>

退出码 0 = 全部字段符合 G2 发布清单；非 0 = 列出不符合项（禁止发布）。
"""

from __future__ import annotations

import argparse
import json
import sys

PROFILE = "r001-independent-v1"
VEHICLE = "firebot-vehicle-01"
G2_KEYS = (
    "VEHICLE_AGENT_V1_DOWNLINK_ENABLED",
    "VEHICLE_AGENT_V1_DOWNLINK_ALLOWLIST",
    "VEHICLE_TRANSPORT_PROFILES",
    "TRANSPORT_PROFILE_ADAPTERS",
    "VEHICLE_EXECUTION_PROFILES",
)


def ports(service: dict) -> list[tuple[str, str]]:
    result = []
    for item in service.get("ports") or []:
        if isinstance(item, dict):
            result.append((str(item.get("published")), str(item.get("target"))))
        else:
            parts = str(item).split(":")
            result.append((parts[-2], parts[-1]))
    return result


G2_MAIN_SERVICES = {"api", "command-dispatcher", "web"}


def verify(
    main: dict, r001: dict, sha: str | None, adapter_id: str, baseline: dict | None = None
) -> list[str]:
    problems: list[str] = []
    ms, rs = main["services"], r001["services"]

    def env(services, name):
        return services.get(name, {}).get("environment") or {}

    # 1) 五服务镜像：同一 G2 SHA，无 latest
    images = {
        "api": ms.get("api", {}).get("image"),
        "command-dispatcher": ms.get("command-dispatcher", {}).get("image"),
        "web": ms.get("web", {}).get("image"),
        "r001-adapter": rs.get("r001-adapter", {}).get("image"),
        "r001-ingress": rs.get("r001-ingress", {}).get("image"),
    }
    for name, image in images.items():
        if not image or image.endswith(":latest") or ":" not in image:
            problems.append(f"{name}: image {image!r} not pinned")
        elif sha and sha[:7] not in image:
            problems.append(f"{name}: image {image} does not carry G2 SHA {sha[:7]}")
    python_images = {
        images[k] for k in ("api", "command-dispatcher", "r001-adapter", "r001-ingress")
    }
    if len(python_images) != 1:
        problems.append(f"python services on different images: {sorted(python_images)}")
    # 2) 三侧 G2 配置一致
    sides = {
        "api": env(ms, "api"),
        "command-dispatcher": env(ms, "command-dispatcher"),
        "r001-adapter": env(rs, "r001-adapter"),
    }
    for key in G2_KEYS:
        values = {side: values.get(key) for side, values in sides.items()}
        if len(set(values.values())) != 1:
            problems.append(f"{key} differs across sides: {values}")
    adapter_env = sides["r001-adapter"]
    if adapter_env.get("FIREBOT_SERVICE_INSTANCE_ID") != adapter_id:
        problems.append(
            f"r001-adapter instance id {adapter_env.get('FIREBOT_SERVICE_INSTANCE_ID')!r} "
            f"!= {adapter_id}"
        )
    if f"{PROFILE}={adapter_id}" not in (adapter_env.get("TRANSPORT_PROFILE_ADAPTERS") or ""):
        problems.append("TRANSPORT_PROFILE_ADAPTERS does not name the dedicated adapter instance")
    if adapter_env.get("VEHICLE_AGENT_TRANSPORT_PROFILE") != PROFILE:
        problems.append("r001-adapter VEHICLE_AGENT_TRANSPORT_PROFILE != r001-independent-v1")
    if (adapter_env.get("VEHICLE_AGENT_V1_DOWNLINK_ALLOWLIST") or "") != VEHICLE:
        problems.append("downlink allowlist must be exactly firebot-vehicle-01")
    # 3) 原 vehicle-agent-adapter 不在 G2 下行面：不得持有 allowlist
    legacy = env(ms, "vehicle-agent-adapter")
    if legacy.get("VEHICLE_AGENT_V1_DOWNLINK_ALLOWLIST"):
        problems.append("legacy vehicle-agent-adapter must not carry a downlink allowlist")
    # 4) Dispatcher 网络与凭证
    dispatcher = ms.get("command-dispatcher", {})
    networks = dispatcher.get("networks") or {}
    if "r001" not in networks or "firebot" not in networks:
        problems.append(f"command-dispatcher networks {sorted(networks)} must include firebot+r001")
    mounts = " ".join(
        str(v.get("target") if isinstance(v, dict) else v) for v in dispatcher.get("volumes") or []
    )
    if "/run/r001-secrets/r001-platform-pw" not in mounts:
        problems.append("command-dispatcher lacks the r001 platform password mount")
    if "dispatcher-broker-profiles.json" not in mounts:
        problems.append("command-dispatcher lacks dispatcher-broker-profiles.json mount")
    # 5) 端口：原 Broker 8883 / 专属 Broker 8884 不变
    if ("8884", "8883") not in ports(rs.get("r001-broker", {})):
        problems.append(f"r001-broker ports {ports(rs.get('r001-broker', {}))} != 8884->8883")
    if not any(target == "8883" for _, target in ports(ms.get("mosquitto", {}))):
        problems.append("main mosquitto 8883 listener missing")
    # 6) 项目名：两栈不得同名（避免 up 时互相重建）
    if main.get("name") == r001.get("name"):
        problems.append("main and r001 compose project names collide")
    # 7) 非 G2 服务（含 002 链路 mosquitto / mqtt-ingress / vehicle-agent-adapter）展开配置
    #    必须与发布前基线逐字段一致 → 不会被 compose 重建
    if baseline is not None:
        for name, before in baseline["services"].items():
            if name in G2_MAIN_SERVICES:
                continue
            after = ms.get(name)
            if after != before:
                changed = sorted(
                    key
                    for key in set(before) | set(after or {})
                    if (after or {}).get(key) != before.get(key)
                )
                problems.append(f"non-G2 service {name} changed: {changed}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_json")
    parser.add_argument("r001_json")
    parser.add_argument("--sha")
    parser.add_argument("--adapter-id", default="r001-dedicated-adapter-v2")
    parser.add_argument(
        "--baseline", help="发布前（不含 G2 overlay/g2.env）主栈 config --format json"
    )
    args = parser.parse_args()
    with open(args.main_json, encoding="utf-8") as handle:
        main_cfg = json.load(handle)
    with open(args.r001_json, encoding="utf-8") as handle:
        r001_cfg = json.load(handle)
    baseline = None
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as handle:
            baseline = json.load(handle)
    problems = verify(main_cfg, r001_cfg, args.sha, args.adapter_id, baseline)
    for problem in problems:
        print("G2_COMPOSE_FAIL", problem)
    print("G2_COMPOSE_VERIFY=" + ("PASS" if not problems else "FAIL"))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
