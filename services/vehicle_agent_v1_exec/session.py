"""车端命令会话合同：独立校验 + 幂等去重（不依赖云端校验结果）。

校验顺序（每条命令）：
1. 结构（WireCommand，extra=forbid）；无法解析 msg_id → 丢弃（无法关联 ACK）。
2. 幂等：同 msg_id 已有结果 → 原样重发原 ACK/反馈，不重复执行；执行中 → 忽略。
3. 会话合同四字段必须齐全（vehicle_id/target_boot_id/expires_at/mode）。
4. vehicle_id == 本车，否则 WRONG_VEHICLE。
5. target_boot_id == 本车当前会话，否则 STALE_BOOT（重启/重连后上一会话命令不执行）。
6. expires_at 已过 → EXPIRED；ts 明显超前本机时钟（> MAX_FUTURE_SKEW）→ EXPIRED（时钟不可信）。
7. 授权：执行档不含该命令 → NOT_AUTHORIZED；命令模式与授权模式不一致 → MODE_MISMATCH。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from services.vehicle_agent_v1 import WireCommand

MAX_FUTURE_SKEW = timedelta(seconds=5)
DEDUP_TTL_SECONDS = 600.0
DEDUP_MAX_ENTRIES = 500


@dataclass
class Verdict:
    msg_id: str | None
    wire: WireCommand | None = None
    reject_code: str | None = None
    reject_message: str = ""
    replay: dict | None = None
    in_progress: bool = False
    mode: str | None = None


def fingerprint(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


class DedupStore:
    """msg_id → {fp, ack, events, at}；落盘（原子替换）以覆盖进程重启后的 QoS1 重投。"""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        self.inflight: set[str] = set()
        if path and path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.entries = {}
        self._prune()

    def _prune(self) -> None:
        now = time.time()
        self.entries = {
            k: v for k, v in self.entries.items() if now - v.get("at", 0) < DEDUP_TTL_SECONDS
        }
        if len(self.entries) > DEDUP_MAX_ENTRIES:
            ordered = sorted(self.entries.items(), key=lambda item: item[1]["at"])
            self.entries = dict(ordered[-DEDUP_MAX_ENTRIES:])

    def _flush(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def claim(self, msg_id: str, fp: str) -> tuple[str, dict | None]:
        """返回 ("new"|"replay"|"inflight"|"conflict", entry)。"""
        with self.lock:
            self._prune()
            entry = self.entries.get(msg_id)
            if entry is not None:
                return ("replay" if entry["fp"] == fp else "conflict"), entry
            if msg_id in self.inflight:
                return "inflight", None
            self.inflight.add(msg_id)
            return "new", None

    def record_ack(self, msg_id: str, fp: str, ack: dict) -> None:
        with self.lock:
            self.entries[msg_id] = {"fp": fp, "ack": ack, "events": [], "at": time.time()}
            self._flush()

    def record_event(self, msg_id: str, event: dict) -> None:
        with self.lock:
            if msg_id in self.entries:
                self.entries[msg_id]["events"].append(event)
                self._flush()

    def release(self, msg_id: str) -> None:
        with self.lock:
            self.inflight.discard(msg_id)


class SessionValidator:
    def __init__(self, vehicle_id: str, grant, dedup: DedupStore) -> None:
        self.vehicle_id = vehicle_id
        self.grant = grant
        self.dedup = dedup
        self._boot_id: str | None = None
        self._lock = threading.Lock()

    def new_session(self) -> str:
        """每个 MQTT 会话（连接/重连）生成新的会话 UUID；上一会话命令随之失效。"""
        with self._lock:
            self._boot_id = str(__import__("uuid").uuid4())
            return self._boot_id

    @property
    def boot_id(self) -> str | None:
        with self._lock:
            return self._boot_id

    def check(self, raw: dict, now: datetime | None = None) -> Verdict:
        now = now or datetime.now(UTC)
        try:
            msg_id = str(UUID(str(raw.get("msg_id"))))
        except (ValueError, TypeError, AttributeError):
            return Verdict(msg_id=None, reject_code="DROP", reject_message="unparseable msg_id")
        fp = fingerprint(raw)
        state, entry = self.dedup.claim(msg_id, fp)
        if state == "replay":
            return Verdict(msg_id=msg_id, replay=entry)
        if state == "inflight":
            return Verdict(msg_id=msg_id, in_progress=True)
        if state == "conflict":
            # 同 msg_id 不同内容：拒绝且不覆盖原结果
            return Verdict(
                msg_id=msg_id, reject_code="INVALID_PARAM", reject_message="msg_id reused"
            )
        try:
            wire = WireCommand.model_validate(raw)
        except ValidationError:
            return self._reject(msg_id, "INVALID_PARAM", "command does not match v1 contract")
        if None in (wire.vehicle_id, wire.target_boot_id, wire.expires_at, wire.mode):
            return self._reject(msg_id, "INVALID_PARAM", "missing session contract fields", wire)
        if wire.vehicle_id != self.vehicle_id:
            return self._reject(msg_id, "WRONG_VEHICLE", "command targets another vehicle", wire)
        if str(wire.target_boot_id) != self.boot_id:
            return self._reject(msg_id, "STALE_BOOT", "command targets another session", wire)
        if wire.expires_at <= now:
            return self._reject(msg_id, "EXPIRED", "command expired before execution", wire)
        if wire.ts - now > MAX_FUTURE_SKEW:
            return self._reject(
                msg_id, "EXPIRED", "command issued in the future (clock skew)", wire
            )
        if not self.grant.valid(now):
            return self._reject(msg_id, "NOT_AUTHORIZED", "execution grant expired", wire)
        mode = self.grant.mode_for(wire.command)
        if mode is None:
            return self._reject(msg_id, "NOT_AUTHORIZED", "command not in execution profile", wire)
        if wire.mode != mode:
            return self._reject(msg_id, "MODE_MISMATCH", f"vehicle mode is {mode}", wire)
        return Verdict(msg_id=msg_id, wire=wire, mode=mode)

    def _reject(self, msg_id: str, code: str, message: str, wire=None) -> Verdict:
        return Verdict(msg_id=msg_id, wire=wire, reject_code=code, reject_message=message)
