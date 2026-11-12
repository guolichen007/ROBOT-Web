"""vehicle-agent-v1-exec 入口（显式授权的双向执行模式）。

用法::

    PYTHONPATH=. python3 -m services.vehicle_agent_v1_exec.main \\
        --config /etc/firebot/vehicle-agent-v1-readonly/device_identity.json \\
        --grant /etc/firebot/vehicle-agent-v1-exec/execution_grant.json \\
        --run-dir /var/lib/firebot/vehicle-agent-v1-readonly

唯一通信 owner：与 readonly Agent 共用同一 run-dir 的 agent.lock（flock 互斥）——同一设备
身份同一时刻只能有一个进程（readonly 或 exec）持有 MQTT 会话；旧 vehicle-bridge 存在即拒绝。
退出码：0 正常；2 配置/授权错误；3 owner 冲突。
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from services.vehicle_agent_v1_exec import AGENT_NAME, AGENT_ROLE, AGENT_VERSION
from services.vehicle_agent_v1_exec.agent import DuplexAgent
from services.vehicle_agent_v1_exec.executor import (
    BRIDGE_CONNECT_TIMEOUT_SECONDS,
    CommandExecutor,
)
from services.vehicle_agent_v1_exec.grant import GrantError, load_grant
from services.vehicle_agent_v1_exec.ros.software_bridge import request as bridge_request
from services.vehicle_agent_v1_exec.session import DedupStore, SessionValidator
from services.vehicle_agent_v1_exec.transport import DuplexTransport
from services.vehicle_agent_v1_readonly.guard import OwnerConflictError, OwnerGuard
from services.vehicle_agent_v1_readonly.identity import IdentityError, load_identity
from services.vehicle_agent_v1_readonly.main import _default_markers

EXIT_CONFIG = 2
EXIT_OWNER_CONFLICT = 3
FEEDBACK_READ_TIMEOUT_SECONDS = 70.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=AGENT_NAME, description=f"{AGENT_NAME} {AGENT_VERSION}")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--grant", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--status-interval", type=float, default=3.0)
    parser.add_argument("--allow-placeholder-credentials", action="store_true")
    parser.add_argument("--no-tls", action="store_true", help="仅本地隔离测试（生产禁止）")
    return parser


def make_bridge_client(socket_path: Path):
    def run(payload: dict):
        yield from bridge_request(str(socket_path), payload, FEEDBACK_READ_TIMEOUT_SECONDS)

    def status() -> dict | None:
        try:
            for item in bridge_request(
                str(socket_path), {"op": "status"}, BRIDGE_CONNECT_TIMEOUT_SECONDS
            ):
                value = item.get("status")
                age = item.get("age_s")
                if value is None or age is None or age > 10:
                    return {"state": "UNKNOWN", "hardware": "NO_HARDWARE", "stale": True}
                return value
        except (OSError, ValueError):
            return {"state": "BRIDGE_UNAVAILABLE", "hardware": "NO_HARDWARE"}
        return None

    return run, status


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger = logging.getLogger(AGENT_NAME)
    try:
        identity = load_identity(
            args.config, allow_placeholder_password=args.allow_placeholder_credentials
        )
        grant = load_grant(args.grant, identity.vehicle_id)
    except (IdentityError, GrantError) as exc:
        logger.error("refusing to start execution mode: %s", exc)
        return EXIT_CONFIG
    run_dir = args.run_dir.resolve()
    guard = OwnerGuard(run_dir)
    try:
        guard.acquire()
    except OwnerConflictError as exc:
        logger.error("refusing to start: %s", exc)
        return EXIT_OWNER_CONFLICT
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    validator = SessionValidator(
        identity.vehicle_id, grant, DedupStore(run_dir / "exec-dedup.json")
    )
    transport = DuplexTransport(identity, on_session=validator.new_session, tls=not args.no_tls)
    run_bridge = software_status = None
    if grant.ros_bridge_socket is not None:
        run_bridge, software_status = make_bridge_client(grant.ros_bridge_socket)
    executor = CommandExecutor(
        validator,
        lambda kind, data, **kw: transport.publish(kind, data, **kw),
        program_markers=_default_markers(),
        software_status=software_status,
        bridge_request=run_bridge,
        execution_log=run_dir / "exec-log.jsonl",
    )
    agent = DuplexAgent(
        transport,
        validator,
        executor,
        _default_markers(),
        stop_event,
        status_interval=args.status_interval,
    )
    logger.info(
        "%s %s (%s) as %s profile=%s broker=%s:%s grant_until=%s",
        AGENT_NAME,
        AGENT_VERSION,
        AGENT_ROLE,
        identity.vehicle_id,
        grant.execution_profile,
        identity.mqtt_host,
        identity.mqtt_port,
        json.dumps(grant.not_after.isoformat()),
    )
    logger.info("safety: no motor/CAN/relay/GPIO path; REAL commands answer NO_HARDWARE")
    try:
        return agent.run()
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
