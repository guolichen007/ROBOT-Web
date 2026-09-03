"""一次性 enrollment token 存储（文件 + 加锁，支持 expiry + 单次消费 + 重放拒绝）。

安全属性：
- issue：生成随机 token，只存 SHA-256 哈希（不存明文）。
- consume：校验哈希匹配 + DEVICE_ID 绑定 + 未过期 + 未消费；成功即标记 consumed。
- 第二次使用同一 token → consume 返回 False（重放拒绝）。
- 过期 token → consume 返回 False。

生产可替换为 DB 表；本实现用文件 + fcntl 锁，避免引入 migration。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time

DEFAULT_STORE_DIR = "/opt/firebot/enrollment"
DEFAULT_TTL_SECONDS = 3600  # 1 小时一次性 token


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    def __init__(self, store_dir: str = DEFAULT_STORE_DIR, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.store_dir = store_dir
        self.ttl_seconds = ttl_seconds

    def _path(self, device_id: str) -> str:
        return os.path.join(self.store_dir, f"{device_id}.json")

    def issue(self, device_id: str) -> str:
        """签发一次性 token，返回明文 token（只存哈希）。"""
        os.makedirs(self.store_dir, exist_ok=True)
        token = secrets.token_hex(32)
        record = {
            "device_id": device_id,
            "token_hash": _hash(token),
            "issued_at": time.time(),
            "expires_at": time.time() + self.ttl_seconds,
            "consumed": False,
        }
        with open(self._path(device_id), "w", encoding="utf-8") as f:
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        return token

    def _read(self, device_id: str) -> dict | None:
        try:
            with open(self._path(device_id), encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def consume(self, device_id: str, token: str) -> bool:
        """验证并消费一次性 token。

        成功（哈希匹配 + 绑定 DEVICE_ID + 未过期 + 未消费）→ 标记 consumed，返回 True。
        重放 / 过期 / 不匹配 / 已消费 → False。
        """
        record = self._read(device_id)
        if not record:
            return False
        if record.get("device_id") != device_id:
            return False
        if record.get("consumed"):
            return False
        if time.time() > record.get("expires_at", 0):
            return False
        if not secrets.compare_digest(record.get("token_hash", ""), _hash(token)):
            return False
        # 原子消费（先写 consumed，再落盘）
        record["consumed"] = True
        try:
            with open(self._path(device_id), "w", encoding="utf-8") as f:
                json.dump(record, f)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            return False
        return True
