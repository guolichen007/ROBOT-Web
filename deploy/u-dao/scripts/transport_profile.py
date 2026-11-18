#!/usr/bin/env python3
"""Transport Profile 解析器（服务器侧，标准库实现；fleet-register / 媒体登记共用）。

一个车辆的接入参数必须来自同一个批准 Profile，且以下字段一致：
  host / port / protocol / CA / broker_ref / adapter / credential provisioner
解析规则（fail-closed，绝不回退默认值）：
  1. 注册表 vehicles 中显式映射 → 使用该 Profile；若同时给了 --profile 且不同 → 拒绝（PROFILE_CONFLICT）；
  2. 未映射：必须显式 --profile，且该 Profile assignable_to_new_vehicles=true；否则 PROFILE_REQUIRED；
  3. host 为 @activation:<K1>|<K2> 时从 public-activation.env 解析；缺失/私网/占位 → HOST_INVALID；
  4. 需要凭证签发（--require-provisioner）时，Profile 无 provisioner → NO_PROVISIONER；
     provisioner.broker_ref 必须等于 Profile.broker_ref（凭证写入正确 Broker）→ 否则 BROKER_MISMATCH。

用法：
  transport_profile.py resolve <VEHICLE_ID> [--profile <ID>] [--activation-env <F>]
                       [--registry <F>] [--require-provisioner] [--format json|env]
退出码：0 成功；3 解析失败（stderr 一行 TRANSPORT_PROFILE=FAIL reason=<CODE> ...）；2 用法。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REGISTRY = ROOT / "deploy" / "u-dao" / "transport-profiles.json"
VEHICLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
BAD_HOST = re.compile(
    r"(^|\.)(localhost)$|^127\.|^10\.|^192\.168\.|^172\.(1[6-9]|2[0-9]|3[01])\.|^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|^169\.254\.|"
    r"tailscale|example|REPLACE_|CHANGE_ME|<",
    re.I,
)
REQUIRED = ("host", "port", "protocol", "broker_ref", "ca", "adapter")
# 批准的协议（服务器侧白名单；车端 Vehicle Edge 只承载 canonical-mqtt）
APPROVED_PROTOCOLS = frozenset({"canonical-mqtt-1.3", "vehicle-agent-v1"})
# 凭证签发器在代码中硬绑定：id → (实际写入的 Broker, 命令, 适用协议)。注册表只能引用这里已知的
# provisioner，且 broker_ref/command/protocol 必须与硬绑定完全一致——防止新增 Profile 把凭证写进
# U-Dao 主 Broker 却下发另一个 Broker 的端口（凭证写入错误 Broker）。
PROVISIONERS = {
    "u-dao-mosquitto-onboard": {
        "broker_ref": "u-dao-firebot/mosquitto",
        "command": "deploy/u-dao/scripts/60-onboard-vehicle.sh",
        "protocol": "canonical-mqtt-1.3",
        "port": 8883,
    },
}


class ProfileError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def load_registry(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError("REGISTRY_INVALID", f"注册表不可读: {exc}") from None
    if data.get("schema") != "firebot.transport-profiles.v1":
        raise ProfileError("REGISTRY_INVALID", "注册表 schema 不匹配")
    for pid, prof in data.get("profiles", {}).items():
        if not PROFILE_RE.fullmatch(pid):
            raise ProfileError("REGISTRY_INVALID", f"profile id 非法: {pid}")
        missing = [k for k in REQUIRED if k not in prof or prof[k] in (None, "")]
        if missing:
            raise ProfileError("REGISTRY_INVALID", f"profile {pid} 缺少字段 {missing}")
        if prof["protocol"] not in APPROVED_PROTOCOLS:
            raise ProfileError(
                "PROTOCOL_NOT_APPROVED", f"profile {pid} protocol {prof['protocol']} 未批准"
            )
        prov = prof.get("credential_provisioner")
        if prov is not None:
            bound = PROVISIONERS.get(prov.get("id", ""))
            if bound is None:
                raise ProfileError("PROVISIONER_UNKNOWN", f"profile {pid} 引用未知 provisioner")
            if (
                prov.get("command") != bound["command"]
                or prov.get("broker_ref") != bound["broker_ref"]
                or prof["broker_ref"] != bound["broker_ref"]
                or prof["protocol"] != bound["protocol"]
                or prof["port"] != bound["port"]
            ):
                raise ProfileError(
                    "BROKER_MISMATCH",
                    f"profile {pid} 与 provisioner {prov.get('id')} 的硬绑定"
                    f"（broker/command/protocol/port）不一致",
                )
        if prov is not None and prov.get("broker_ref") != prof["broker_ref"]:
            raise ProfileError(
                "BROKER_MISMATCH",
                "profile {} provisioner.broker_ref({}) != broker_ref({})".format(
                    pid, prov.get("broker_ref"), prof["broker_ref"]
                ),
            )
    for vid, pid in data.get("vehicles", {}).items():
        if pid not in data.get("profiles", {}):
            raise ProfileError("REGISTRY_INVALID", f"车辆 {vid} 映射到未知 profile {pid}")
    return data


def read_activation(path: Path | None) -> dict:
    out: dict = {}
    if path is None or not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_host(spec: str, activation: dict) -> str:
    if spec.startswith("@activation:"):
        host = ""
        for key in spec[len("@activation:") :].split("|"):
            if activation.get(key):
                host = activation[key].strip()
                break
    else:
        host = spec.strip()
    if not host or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) or BAD_HOST.search(host):
        raise ProfileError("HOST_INVALID", f"正式公网 host 缺失或为私网/Tailscale/占位: {host!r}")
    return host


def resolve(
    vehicle_id: str,
    *,
    profile: str | None = None,
    registry: Path = DEFAULT_REGISTRY,
    activation_env: Path | None = None,
    require_provisioner: bool = False,
) -> dict:
    if not VEHICLE_RE.fullmatch(vehicle_id):
        raise ProfileError("VEHICLE_INVALID", "vehicle_id 非法")
    data = load_registry(registry)
    mapped = data.get("vehicles", {}).get(vehicle_id)
    if mapped and profile and profile != mapped:
        raise ProfileError(
            "PROFILE_CONFLICT", f"{vehicle_id} 已批准映射到 {mapped}，拒绝改用 {profile}"
        )
    pid = mapped or profile
    if not pid:
        raise ProfileError(
            "PROFILE_REQUIRED", f"{vehicle_id} 未在注册表映射，必须显式 --transport-profile"
        )
    prof = data["profiles"].get(pid)
    if prof is None:
        raise ProfileError("PROFILE_UNKNOWN", f"未知 profile {pid}")
    if not mapped and not prof.get("assignable_to_new_vehicles"):
        raise ProfileError("PROFILE_NOT_ASSIGNABLE", f"profile {pid} 不允许分配给新车")
    port = prof["port"]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ProfileError("REGISTRY_INVALID", f"profile {pid} port 非法")
    host = resolve_host(str(prof["host"]), read_activation(activation_env))
    prov = prof.get("credential_provisioner")
    if require_provisioner and not prov:
        raise ProfileError(
            "NO_PROVISIONER",
            f"profile {pid} 无平台凭证签发器（{vehicle_id} 的凭证不由 fleet register 生成；禁止重新 enroll/轮换）",
        )
    return {
        "vehicle_id": vehicle_id,
        "transport_profile": pid,
        "mapped": bool(mapped),
        "protected": vehicle_id in data.get("protected_vehicles", []),
        "mqtt_host": host,
        "mqtt_port": port,
        "protocol": prof["protocol"],
        "broker_ref": prof["broker_ref"],
        "ca_server_path": prof["ca"]["server_path"],
        "ca_vehicle_path": prof["ca"]["vehicle_path"],
        "adapter": prof["adapter"],
        "credential_provisioner": prov["id"] if prov else None,
        "provisioner_command": prov["command"] if prov else None,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="transport_profile.py")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("resolve")
    r.add_argument("vehicle_id")
    r.add_argument("--profile")
    r.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    r.add_argument("--activation-env")
    r.add_argument("--require-provisioner", action="store_true")
    r.add_argument("--format", choices=("json", "env"), default="json")
    args = ap.parse_args(argv)
    if args.cmd != "resolve":
        ap.print_help(sys.stderr)
        return 2
    try:
        res = resolve(
            args.vehicle_id,
            profile=args.profile,
            registry=Path(args.registry),
            activation_env=Path(args.activation_env) if args.activation_env else None,
            require_provisioner=args.require_provisioner,
        )
    except ProfileError as exc:
        print(f"TRANSPORT_PROFILE=FAIL reason={exc.code} {exc}", file=sys.stderr)
        return 3
    if args.format == "env":
        for k, v in res.items():
            print("TP_{}={}".format(k.upper(), "" if v is None else v))
    else:
        print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
