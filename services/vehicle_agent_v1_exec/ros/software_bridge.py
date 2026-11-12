"""本地 ROS 软件桥：执行器 ⇄ Unix socket ⇄ ROS topic。

执行器（Python 3.11 venv，无 rospy）通过本地 Unix socket 提交 SOFTWARE_TEST 命令；本桥
（系统 Python + rospy）发布到 <ns>/command，订阅 <ns>/feedback 与 <ns>/status，按
request_id 精确关联后把反馈逐行回传。只做转发，不执行任何本地动作、不触碰硬件。

socket 协议（每连接一请求，行分隔 JSON）：
  → {"op":"command", ...ROS command 字段...}   ← 反馈行……直至终态或超时
  → {"op":"status"}                            ← {"status": <最近一次 status|null>, "age_s"}
无 ROS 订阅者（软件节点未运行）→ 立即回 {"state":"REJECTED","reason_code":"ROS_UNAVAILABLE"}，
绝不假装已投递。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import socket
import socketserver
import threading
import time
from datetime import datetime, timezone

try:
    from .contract import INTERFACE_VERSION, SOFTWARE_COMMANDS, TERMINAL_STATES, topics
except ImportError:  # 以脚本方式运行（rosrun / systemd 直接执行）
    from contract import (  # type: ignore
        INTERFACE_VERSION,
        SOFTWARE_COMMANDS,
        TERMINAL_STATES,
        topics,
    )

LOG = logging.getLogger("firebot-ros-software-bridge")
SUBSCRIBER_WAIT_SECONDS = 3.0
ACCEPT_TIMEOUT_SECONDS = 5.0
FEEDBACK_TIMEOUT_SECONDS = 60.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RosSoftwareBridge:
    def __init__(self, rospy, string_type, namespace: str) -> None:
        self.rospy = rospy
        self.String = string_type
        self.topics = topics(namespace)
        self.waiters = {}
        self.lock = threading.Lock()
        self.last_status = None
        self.last_status_at = 0.0
        self.publisher = rospy.Publisher(self.topics["command"], string_type, queue_size=10)
        self.feedback_sub = rospy.Subscriber(
            self.topics["feedback"], string_type, self._on_feedback, queue_size=50
        )
        rospy.Subscriber(self.topics["status"], string_type, self._on_status, queue_size=5)

    def _on_feedback(self, message) -> None:
        try:
            data = json.loads(message.data)
        except ValueError:
            LOG.warning("feedback is not JSON; dropped")
            return
        with self.lock:
            target = self.waiters.get(data.get("request_id"))
        if target is None:
            LOG.info("feedback for unknown request_id %s dropped", data.get("request_id"))
            return
        target.put(data)

    def _on_status(self, message) -> None:
        try:
            self.last_status = json.loads(message.data)
            self.last_status_at = time.monotonic()
        except ValueError:
            LOG.warning("status is not JSON; dropped")

    def status(self) -> dict:
        age = time.monotonic() - self.last_status_at if self.last_status_at else None
        return {"status": self.last_status, "age_s": age}

    def _linked(self) -> bool:
        return (
            self.publisher.get_num_connections() > 0 and self.feedback_sub.get_num_connections() > 0
        )

    def run_command(self, request: dict, emit) -> None:
        request_id = request.get("request_id")
        if not request_id or request.get("command") not in SOFTWARE_COMMANDS:
            emit({"request_id": request_id, "state": "REJECTED", "reason_code": "INVALID_PARAM"})
            return
        # 双向链路都须建立：软件节点已订阅 command，且本桥已连上节点的 feedback 发布者；
        # 否则早到的 ACCEPTED/COMPLETED 反馈会在 ROS 层丢失。
        deadline = time.monotonic() + SUBSCRIBER_WAIT_SECONDS
        while time.monotonic() < deadline and not self._linked():
            time.sleep(0.05)
        if not self._linked():
            emit(
                {
                    "request_id": request_id,
                    "state": "REJECTED",
                    "reason_code": "ROS_UNAVAILABLE",
                    "message": "ROS 软件节点未连接 "
                    + self.topics["command"]
                    + " / "
                    + self.topics["feedback"],
                    "ts": now_iso(),
                }
            )
            return
        inbox = queue.Queue()
        with self.lock:
            self.waiters[request_id] = inbox
        try:
            payload = {
                "interface_version": INTERFACE_VERSION,
                "request_id": request_id,
                "command_id": request.get("command_id"),
                "command": request["command"],
                "params": request.get("params") or {},
                "mode": "SOFTWARE_TEST",
                "issued_at": request.get("issued_at"),
                "expires_at": request.get("expires_at"),
            }
            self.publisher.publish(self.String(data=json.dumps(payload)))
            accepted = False
            timeout_at = time.monotonic() + ACCEPT_TIMEOUT_SECONDS
            while True:
                remaining = timeout_at - time.monotonic()
                if remaining <= 0:
                    emit(
                        {
                            "request_id": request_id,
                            "state": "FAILED",
                            "reason_code": "FEEDBACK_TIMEOUT" if accepted else "ROS_UNAVAILABLE",
                            "message": "ROS 软件节点反馈超时",
                            "ts": now_iso(),
                        }
                    )
                    return
                try:
                    feedback = inbox.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                emit(feedback)
                if feedback.get("state") in TERMINAL_STATES:
                    return
                if not accepted:
                    accepted = True
                    timeout_at = time.monotonic() + FEEDBACK_TIMEOUT_SECONDS
        finally:
            with self.lock:
                self.waiters.pop(request_id, None)


def serve(bridge: RosSoftwareBridge, socket_path: str, group_gid=None) -> socketserver.BaseServer:
    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            line = self.rfile.readline(65536)
            try:
                request = json.loads(line)
            except ValueError:
                return

            def emit(item: dict) -> None:
                self.wfile.write((json.dumps(item) + "\n").encode())
                self.wfile.flush()

            if request.get("op") == "status":
                emit(bridge.status())
            elif request.get("op") == "command":
                bridge.run_command(request, emit)

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = Server(socket_path, Handler)
    os.chmod(socket_path, 0o660)
    if group_gid is not None:
        os.chown(socket_path, -1, group_gid)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="firebot-ros-software-bridge")
    parser.add_argument("--namespace", required=True, help="如 /firebot_001/software")
    parser.add_argument("--socket", required=True, help="如 /run/firebot-ros-bridge/bridge.sock")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import rospy
    from std_msgs.msg import String

    rospy.init_node("firebot_software_bridge", anonymous=False, disable_signals=True)
    bridge = RosSoftwareBridge(rospy, String, args.namespace)
    server = serve(bridge, args.socket)
    LOG.info("bridge ready: %s <-> %s", args.socket, bridge.topics)
    try:
        while not rospy.is_shutdown():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        try:
            os.unlink(args.socket)
        except OSError:
            pass
        rospy.signal_shutdown("bridge exit")
    return 0


def request(socket_path: str, payload: dict, timeout: float):
    """执行器侧客户端：返回逐行反馈生成器（连接失败抛 OSError）。"""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(socket_path)
    sock.sendall((json.dumps(payload) + "\n").encode())
    reader = sock.makefile("rb")
    try:
        for line in reader:
            yield json.loads(line)
    finally:
        reader.close()
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
