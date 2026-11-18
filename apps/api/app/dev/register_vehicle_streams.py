"""Ops tool: 媒体流登记 CLI（G4-R1 001 专项入口 + G4-Fleet-R2 通用入口；同一事务核心）。

事务核心：``app.modules.media.registration``（唯一实现）。本文件只做参数解析与入口约束。

1) 001 专项安全入口（保持 G4-R1 语义，无任何参数可改变目标）::

    python -m app.dev.register_vehicle_streams                 # dry-run
    python -m app.dev.register_vehicle_streams --apply > receipt.json
    python -m app.dev.register_vehicle_streams --rollback - [--apply] < receipt.json

2) 通用入口（显式目标；只登记已存在真实资产；Transport Profile 必须可解析）::

    python -m app.dev.register_vehicle_streams generic --vehicle-id firebot-vehicle-03 \\
        --transport-profile u-dao-standard-v1 [--cameras roof_rgb,roof_thermal] [--apply]
    python -m app.dev.register_vehicle_streams generic --vehicle-id firebot-vehicle-03 \\
        --rollback - [--apply] [--confirm-publishers-stopped] < receipt.json

撤销前自动经 MediaMTX API 确认本次创建的流无活动发布会话（有 → 拒绝；无法确认 → 需运维
显式 --confirm-publishers-stopped）。OFFLINE 仅为显示状态，不作为鉴权关闭依据。

退出码：0 成功 / 2 用法 / 3 前置条件（资产不存在/身份或 Profile 不合法） / 4 冲突 / 5 撤销校验失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy.orm import Session

from app.modules.media import registration as core
from app.modules.media.registration import (  # noqa: F401 — 兼容 G4-R1 调用方/测试
    AUDIT_CREATE,
    AUDIT_DELETE,
    EXIT_CONFLICT,
    EXIT_OK,
    EXIT_PRECONDITION,
    EXIT_ROLLBACK,
    EXIT_USAGE,
    REAL_SOURCE_REF,
    SYNTHETIC_SOURCE_REF,
    ToolError,
)

TARGET_VEHICLE = "firebot-vehicle-01"
STREAMS: tuple[tuple[str, str], ...] = (
    (f"{TARGET_VEHICLE}-roof_rgb", "roof_rgb"),
    (f"{TARGET_VEHICLE}-roof_thermal", "roof_thermal"),
)
RECEIPT_KIND = "firebot.g4r1.stream-registration.v1"
GENERIC_RECEIPT_KIND = "firebot.media-stream-registration.v1"


def _target_001() -> core.Target:
    return core.Target(TARGET_VEHICLE, tuple(c for _, c in STREAMS), None)


def _snapshot_protected(db: Session) -> list[dict[str, Any]]:
    return core.snapshot_protected(db, _target_001().protected)


def register(*, apply: bool) -> dict[str, Any]:
    return core.register(_target_001(), apply=apply, receipt_kind=RECEIPT_KIND)


def rollback(receipt: dict[str, Any], *, apply: bool, confirm: bool = False) -> dict[str, Any]:
    return core.rollback(
        _target_001(),
        receipt,
        apply=apply,
        receipt_kind=RECEIPT_KIND,
        confirm_publishers_stopped=confirm,
    )


def _read_receipt(path: str) -> dict[str, Any]:
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("receipt 必须是 JSON 对象")
    return data


def _emit(result: dict[str, Any], tag: str) -> int:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    summary = ",".join(f"{a['stream_id']}={a['action']}" for a in result["actions"])
    print(f"{tag}={result['mode']} {summary}", file=sys.stderr)
    return EXIT_OK


def _fail(exc: ToolError) -> int:
    print(f"ERROR {exc}", file=sys.stderr)
    if exc.detail is not None:
        print(json.dumps(exc.detail, ensure_ascii=False, indent=2), file=sys.stderr)
    return exc.code


def _generic(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m app.dev.register_vehicle_streams generic")
    p.add_argument("--vehicle-id", required=True)
    p.add_argument("--transport-profile")
    p.add_argument("--cameras", default="roof_rgb,roof_thermal")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rollback", metavar="RECEIPT")
    p.add_argument("--confirm-publishers-stopped", action="store_true")
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        if args.vehicle_id == TARGET_VEHICLE:
            raise ToolError(EXIT_USAGE, f"{TARGET_VEHICLE} 使用 001 专项入口（无参数）")
        target = core.Target(
            args.vehicle_id, tuple(c for c in args.cameras.split(",") if c), args.transport_profile
        )
        if args.rollback:
            try:
                receipt = _read_receipt(args.rollback)
            except (OSError, ValueError) as exc:
                raise ToolError(EXIT_USAGE, f"receipt 读取失败: {exc}") from exc
            return _emit(
                core.rollback(
                    target,
                    receipt,
                    apply=args.apply,
                    receipt_kind=GENERIC_RECEIPT_KIND,
                    confirm_publishers_stopped=args.confirm_publishers_stopped,
                ),
                "STREAM_ROLLBACK",
            )
        return _emit(
            core.register(target, apply=args.apply, receipt_kind=GENERIC_RECEIPT_KIND),
            "STREAM_REGISTRATION",
        )
    except ToolError as exc:
        return _fail(exc)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["generic"]:
        return _generic(argv[1:])
    parser = argparse.ArgumentParser(
        prog="python -m app.dev.register_vehicle_streams",
        description=f"登记 {TARGET_VEHICLE} 的 roof_rgb / roof_thermal 媒体流（默认 dry-run）",
    )
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument(
        "--rollback", metavar="RECEIPT", help="撤销：receipt 文件路径，'-' 表示 stdin"
    )
    parser.add_argument("--confirm-publishers-stopped", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        if args.rollback:
            try:
                receipt = _read_receipt(args.rollback)
            except (OSError, ValueError) as exc:
                raise ToolError(EXIT_USAGE, f"receipt 读取失败: {exc}") from exc
            return _emit(
                rollback(receipt, apply=args.apply, confirm=args.confirm_publishers_stopped),
                "STREAM_ROLLBACK",
            )
        return _emit(register(apply=args.apply), "STREAM_REGISTRATION")
    except ToolError as exc:
        return _fail(exc)


if __name__ == "__main__":
    raise SystemExit(main())
