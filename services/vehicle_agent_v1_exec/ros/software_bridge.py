"""本地 ROS1 软件桥（G2-R1）：执行器 ⇄ Unix socket ⇄ 001 正式 ROS 接口。

直接使用 001 现场节点的自定义消息（默认 firebot_001_software/{SoftwareCommand,
SoftwareFeedback,SoftwareStatus}；--message-package 可配置），不要求 001 更换软件节点。
只做转发与关联，不执行任何本地动作、不触碰硬件。

关联规则：
- SoftwareCommand.command_id = V1 msg_id（UUID）。反馈按 command_id 精确关联到请求。
- 停止语义：001 节点对 stop_patrol 只回 ACCEPTED（stop 自己的 command_id），随后巡检线程以
  **patrol 的 command_id** 发布 STOPPED。桥把该 STOPPED 原样交给 patrol 请求（patrol 终态
  STOPPED，记录保留），并为已被接受的 stop_patrol 请求生成以 **stop 自己 msg_id** 关联的
  COMPLETED（ros_status=STOPPED，stopped_command_id=patrol msg_id）。若巡检先自然完成，
  stop 以 COMPLETED/ALREADY_STOPPED 收敛；不以超时代替已停止的事实。

socket 协议（每连接一请求，行分隔 JSON）：
  → {"op":"command","command_id":<msg_id>,"command":...,"params":{...}}  ← 反馈行……直至终态
  → {"op":"status"}  ← {"status": <SoftwareStatus 转换结果|null>, "age_s": float|null}
"""

from __future__ import annotations

import argparse
import importlib
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
    from .contract import (
        DEFAULT_MESSAGE_PACKAGE,
        SOFTWARE_COMMANDS,
        TERMINAL_STATES,
        build_command,
        feedback_to_dict,
        status_to_dict,
        topics,
    )
except ImportError:  # 以脚本方式运行
    from contract import (  # type: ignore
        DEFAULT_MESSAGE_PACKAGE,
        SOFTWARE_COMMANDS,
        TERMINAL_STATES,
        build_command,
        feedback_to_dict,
        status_to_dict,
        topics,
    )

LOG = logging.getLogger("firebot-ros-software-bridge")
SUBSCRIBER_WAIT_SECONDS = 3.0
ACCEPT_TIMEOUT_SECONDS = 5.0
# 无反馈空闲上限（巡检每轮都有 RUNNING，按“最后一次反馈后”计时，而非总时长）
FEEDBACK_IDLE_TIMEOUT_SECONDS = 30.0
MAX_CONCURRENT_REQUESTS = 8


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_message_types(package: str):
    module = importlib.import_module(package + ".msg")
    return module.SoftwareCommand, module.SoftwareFeedback, module.SoftwareStatus


class Pending:
    def __init__(self, command_id: str, action: str) -> None:
        self.command_id = command_id
        self.action = action
        self.inbox: queue.Queue = queue.Queue()
        self.accepted = False
        # stop_patrol 专用：发送时绑定的当前活动 patrol（V1 msg_id）；None = 无可关联巡检
        self.target: str | None = None
        # 目标巡检在本 stop 被 ACCEPTED 之前已结束（STOPPED/COMPLETED），记录原始状态
        self.target_ended_before_accept: str | None = None
        self.resolved = False


def trusted(data: dict) -> bool:
    """可信软件反馈：software_test=true 且非适配器判定的不可信结果。"""
    return data.get("software_test") is True and data.get("reason_code") != (
        "SOFTWARE_TEST_FLAG_MISSING"
    )


class RosSoftwareBridge:
    def __init__(self, rospy, types, namespace: str) -> None:
        self.rospy = rospy
        self.Command, self.Feedback, self.Status = types
        self.topics = topics(namespace)
        self.pending: dict[str, Pending] = {}
        self.lock = threading.Lock()
        self.last_status = None
        self.last_status_at = 0.0
        self.publisher = rospy.Publisher(self.topics["command"], self.Command, queue_size=10)
        self.feedback_sub = rospy.Subscriber(
            self.topics["feedback"], self.Feedback, self._on_feedback, queue_size=100
        )
        rospy.Subscriber(self.topics["status"], self.Status, self._on_status, queue_size=5)

    # ---------------- ROS 回调 ----------------
    def _on_feedback(self, message) -> None:
        data = feedback_to_dict(message)
        outcomes: list[tuple[Pending, dict]] = []
        with self.lock:
            target = self.pending.get(data["command_id"])
            if target is not None and target.action != data["action"]:
                # command_id 与 action 不一致：不可信串联，按失败交给该请求
                data = {**data, "state": "FAILED", "reason_code": "FEEDBACK_ACTION_MISMATCH"}
            # 1) 巡检终止（仅可信 STOPPED/COMPLETED）→ 只关联绑定了该 patrol 的 stop
            if (
                data["action"] == "patrol"
                and data["ros_status"] in ("STOPPED", "COMPLETED")
                and trusted(data)
            ):
                for stop in self.pending.values():
                    if stop.action != "stop_patrol" or stop.target != data["command_id"]:
                        continue
                    if stop.resolved:
                        continue
                    if not stop.accepted:
                        stop.target_ended_before_accept = data["ros_status"]
                        continue
                    stop.resolved = True
                    outcomes.append((stop, self._stop_outcome(stop, data["ros_status"], data)))
            # 2) stop 自身 ACCEPTED：若目标已提前结束 / 无可关联巡检，立即给出非成功终态
            if (
                target is not None
                and target.action == "stop_patrol"
                and data["state"] == "ACCEPTED"
                and trusted(data)
            ):
                target.accepted = True
                if target.target is None:
                    target.resolved = True
                    outcomes.append(
                        (
                            target,
                            self._stop_result(
                                target,
                                "FAILED",
                                "STOP_TARGET_UNTRACKED",
                                "stop accepted but no tracked active patrol (result unconfirmed)",
                                data,
                            ),
                        )
                    )
                elif target.target_ended_before_accept:
                    target.resolved = True
                    outcomes.append(
                        (
                            target,
                            self._stop_result(
                                target,
                                "REJECTED",
                                "STOP_NOT_EFFECTIVE",
                                "patrol ended ("
                                + target.target_ended_before_accept
                                + ") before stop was accepted",
                                data,
                            ),
                        )
                    )
            elif target is not None and data["state"] == "ACCEPTED" and trusted(data):
                target.accepted = True
        if target is not None:
            target.inbox.put(data)
        else:
            LOG.info("feedback for unknown command_id %s (%s)", data["command_id"], data["state"])
        for stop, result in outcomes:
            stop.inbox.put(result)

    def _stop_outcome(self, stop: Pending, ros_status: str, data: dict) -> dict:
        if ros_status == "STOPPED":
            return {
                **self._stop_result(stop, "COMPLETED", None, data["message"], data),
                "ros_status": "STOPPED",
            }
        # 巡检自然完成与停止竞争：停止未生效，不得报成功
        return self._stop_result(
            stop,
            "REJECTED",
            "PATROL_ALREADY_FINISHED",
            "patrol completed naturally before stop took effect: " + data["message"],
            data,
        )

    @staticmethod
    def _stop_result(stop: Pending, state, reason, message, data) -> dict:
        return {
            "command_id": stop.command_id,
            "action": "stop_patrol",
            "state": state,
            "ros_status": data.get("ros_status"),
            "reason_code": reason,
            "message": message,
            "software_test": True,
            "stopped_command_id": stop.target,
            "source": "bridge-correlation",
            "ts": data.get("ts") or now_iso(),
        }

    def _on_status(self, message) -> None:
        self.last_status = status_to_dict(message)
        self.last_status_at = time.monotonic()

    def status(self) -> dict:
        age = time.monotonic() - self.last_status_at if self.last_status_at else None
        return {"status": self.last_status, "age_s": age}

    def _linked(self) -> bool:
        return (
            self.publisher.get_num_connections() > 0 and self.feedback_sub.get_num_connections() > 0
        )

    # ---------------- 请求处理 ----------------
    def run_command(self, request: dict, emit) -> None:
        command_id = str(request.get("command_id") or "")
        action = request.get("command")
        if not command_id or action not in SOFTWARE_COMMANDS:
            emit(self._local(command_id, action, "REJECTED", "INVALID_PARAM", "bad request"))
            return
        deadline = time.monotonic() + SUBSCRIBER_WAIT_SECONDS
        while time.monotonic() < deadline and not self._linked():
            time.sleep(0.05)
        if not self._linked():
            emit(
                self._local(
                    command_id,
                    action,
                    "REJECTED",
                    "ROS_UNAVAILABLE",
                    f"ROS 软件节点未连接 {self.topics['command']} / {self.topics['feedback']}",
                )
            )
            return
        pending = Pending(command_id, action)
        with self.lock:
            if command_id in self.pending:
                emit(self._local(command_id, action, "REJECTED", "BUSY", "duplicate in-flight"))
                return
            if action == "stop_patrol":
                # 只绑定当前活动 patrol（本桥在途、已被节点 ACCEPTED 的唯一巡检）
                active = [p for p in self.pending.values() if p.action == "patrol" and p.accepted]
                pending.target = active[-1].command_id if len(active) == 1 else None
            self.pending[command_id] = pending
        try:
            self.publisher.publish(
                build_command(self.Command, command_id, action, request.get("params") or {})
            )
            LOG.info("forwarded %s command_id=%s", action, command_id)
            timeout_at = time.monotonic() + ACCEPT_TIMEOUT_SECONDS
            while True:
                remaining = timeout_at - time.monotonic()
                if remaining <= 0:
                    emit(
                        self._local(
                            command_id,
                            action,
                            "FAILED",
                            "FEEDBACK_TIMEOUT" if pending.accepted else "ROS_UNAVAILABLE",
                            "ROS 软件节点反馈超时（结果未确认）",
                        )
                    )
                    return
                try:
                    feedback = pending.inbox.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                emit(feedback)
                if feedback["state"] in TERMINAL_STATES:
                    return
                timeout_at = time.monotonic() + FEEDBACK_IDLE_TIMEOUT_SECONDS
        finally:
            with self.lock:
                self.pending.pop(command_id, None)

    @staticmethod
    def _local(command_id, action, state, reason, message) -> dict:
        return {
            "command_id": command_id,
            "action": action,
            "state": state,
            "ros_status": None,
            "reason_code": reason,
            "message": message,
            "software_test": True,
            "source": "bridge",
            "ts": now_iso(),
        }


def serve(bridge: RosSoftwareBridge, socket_path: str) -> socketserver.BaseServer:
    slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            if not slots.acquire(blocking=False):
                self.wfile.write(b'{"state":"REJECTED","reason_code":"BUSY"}\n')
                return
            try:
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
            finally:
                slots.release()

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = Server(socket_path, Handler)
    os.chmod(socket_path, 0o660)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="firebot-ros-software-bridge")
    parser.add_argument("--namespace", required=True, help="如 /firebot_001/software")
    parser.add_argument("--socket", required=True, help="如 /run/firebot-ros-bridge/bridge.sock")
    parser.add_argument(
        "--message-package",
        default=DEFAULT_MESSAGE_PACKAGE,
        help="ROS 消息包（含 SoftwareCommand/SoftwareFeedback/SoftwareStatus）",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import rospy

    types = load_message_types(args.message_package)
    rospy.init_node("firebot_software_bridge", anonymous=False, disable_signals=True)
    bridge = RosSoftwareBridge(rospy, types, args.namespace)
    server = serve(bridge, args.socket)
    LOG.info("bridge ready: %s <-> %s (%s)", args.socket, bridge.topics, args.message_package)
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
