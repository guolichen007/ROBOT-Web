"""明确隔离的测试运行时：真实 API/Outbox/adapter/ingress/WS，fake broker/Redis。

SQLite fixture 不验证 PostgreSQL 分区、迁移或行锁，不能代替 Compose 验收。
仅显式 FIREBOT_MOCK_E2E=1 + APP_ENV=test 可启动，绝不连接 MQTT socket。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import queue
import threading
import time
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

from fakeredis import FakeRedis
from sqlalchemy import DateTime, create_engine, select
from sqlalchemy.dialects.sqlite import DATETIME

ROOT = Path(__file__).resolve().parents[1]


def load_service(name: str):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "services" / name / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AwareDateTime(DATETIME):
    def result_processor(self, dialect, coltype):
        base = super().result_processor(dialect, coltype)

        def process(value):
            result = base(value) if base else value
            return result.replace(tzinfo=UTC) if result and result.tzinfo is None else result

        return process


class MockRuntime:
    def __init__(self, database_path: Path):
        if os.environ.get("APP_ENV") != "test" or os.environ.get("FIREBOT_MOCK_E2E") != "1":
            raise RuntimeError("explicit isolated mock test environment required")
        from app.core.config import get_settings
        from app.core.events import get_redis
        from app.db import session
        from app.db.models import Base, User

        self.settings = get_settings()
        self.original_settings = self.settings.model_copy(deep=True)
        self.original_bind = session.SessionLocal.kw.get("bind")
        self.settings.app_env = "test"
        self.settings.mock_enabled = True
        self.settings.seed_demo = True
        self.settings.bootstrap_admin_password = "MockOnly-ChangeMe-2026!"
        self.settings.asset_root = database_path.parent / "mock-assets"
        self.settings.allowed_origins = (
            "http://127.0.0.1:18765,http://127.0.0.1:15173,http://localhost:15173"
        )
        self.settings.vehicle_agent_v1_downlink_enabled = True
        self.settings.cookie_secure = False
        self.redis = FakeRedis(decode_responses=True)
        self.redis_patch = patch("app.core.events.Redis.from_url", return_value=self.redis)
        self.redis_patch.start()
        get_redis.cache_clear()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        self.engine.dialect.colspecs = {**self.engine.dialect.colspecs, DateTime: AwareDateTime}
        session.SessionLocal.configure(bind=self.engine)
        self.SessionLocal = session.SessionLocal
        partitioned = {"sensor_samples", "telemetry_samples"}
        Base.metadata.create_all(
            self.engine,
            tables=[t for t in Base.metadata.sorted_tables if t.name not in partitioned],
        )
        # SQLite cannot represent PostgreSQL's partitioned composite identity.
        # These two fixture tables only exercise ORM sample persistence.
        with self.engine.begin() as conn:
            for name in partitioned:
                columns = []
                for column in Base.metadata.tables[name].columns:
                    if column.name == "id":
                        columns.append('"id" INTEGER PRIMARY KEY AUTOINCREMENT')
                    else:
                        columns.append(
                            f'"{column.name}" {column.type.compile(dialect=self.engine.dialect)}'
                        )
                conn.exec_driver_sql(f'CREATE TABLE "{name}" ({", ".join(columns)})')
        from app.db.seed import seed

        seed()
        with self.SessionLocal.begin() as db:
            admin = db.scalar(select(User).where(User.username == "admin"))
            admin.must_change_password = False
        from app.dev.setup_vehicle_agent_mock import main as setup_mock

        setup_mock()
        self.ingress = load_service("mqtt-ingress")
        self.dispatcher = load_service("command-dispatcher")
        adapter_module = load_service("vehicle-agent-adapter")
        self.MockAgent = load_service("mock-vehicle-agent").MockAgent
        self.mock = self.MockAgent()
        self.messages: queue.Queue = queue.Queue()
        self.published: list[tuple] = []
        self.errors: list[str] = []
        self.adapter = adapter_module.Adapter(self.redis, self.publish, downlink=True)

        class Info:
            def wait_for_publish(self, timeout=3):
                return None

            def is_published(self):
                return True

        class Client:
            def publish(client_self, topic, payload, qos=1, retain=False):
                self.publish(topic, payload, qos, retain)
                return Info()

        self.dispatcher.client = Client()
        self.send("availability", "online")
        self.send("status", self.mock.status())
        self.send("telemetry", self.mock.telemetry())
        self.pump()
        from app.main import app

        self.app = app
        self.running = False

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        self.messages.put(
            (topic, payload.encode() if isinstance(payload, str) else payload, retain)
        )

    def send(self, kind, data):
        self.messages.put((f"v1/robot/R001/{kind}", json.dumps(data).encode(), False))

    def pump(self):
        for _ in range(500):
            try:
                topic, payload, retained = self.messages.get_nowait()
            except queue.Empty:
                return
            if topic == "v1/robot/R001/cmd":
                ack = self.mock.execute(json.loads(payload))
                if ack:
                    self.send("ack", ack)
                    self.send("status", self.mock.status())
            elif topic.startswith("_platform/vehicle-agent-v1/") and not topic.endswith("/command"):
                self.ingress.process(topic, payload)
            elif topic.startswith(("v1/", "_platform/vehicle-agent-v1/")):
                self.adapter.process(topic, payload, retained=retained)
        raise RuntimeError("fake broker did not quiesce")

    def cycle(self):
        self.dispatcher.dispatch_next()
        self.pump()
        self.adapter.expire()
        self.pump()

    def start(self):
        self.running = True

        def loop():
            while self.running:
                try:
                    self.send("status", self.mock.status())
                    self.send("telemetry", self.mock.telemetry())
                    self.cycle()
                except Exception as exc:
                    self.errors.append(str(exc))
                    logging.exception("mock runtime cycle failed")
                time.sleep(0.2)

        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def close(self):
        self.running = False
        if hasattr(self, "thread"):
            self.thread.join(timeout=3)
        self.engine.dispose()
        self.SessionLocal.configure(bind=self.original_bind)
        for field in type(self.settings).model_fields:
            setattr(self.settings, field, getattr(self.original_settings, field))
        self.redis_patch.stop()
        from app.core.events import get_redis

        get_redis.cache_clear()


def main():
    import argparse
    import tempfile

    import uvicorn
    from fastapi import Body

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    os.environ["APP_ENV"] = "test"
    os.environ["FIREBOT_MOCK_E2E"] = "1"
    # Fresh temp DB per launch; no production path or connection string is read.
    (ROOT / "artifacts").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vehicle-agent-mock-", dir=ROOT / "artifacts") as temp:
        runtime = MockRuntime(Path(temp) / "mock.sqlite")

        @runtime.app.post("/__mock__/scenario")
        def scenario(payload: dict = Body(...)):
            action = payload.get("action")
            if action == "arrive":
                runtime.mock.arrive()
            elif action in {"offline", "online"}:
                runtime.mock.online = action == "online"
                runtime.send("availability", action)
            elif action == "failure":
                code = payload.get("code")
                if code not in {None, "BUSY", "NOT_READY", "TIMEOUT", "START_FAILED"}:
                    raise ValueError("unsupported fake failure")
                runtime.mock.failure = code
            return {"ok": True}

        runtime.start()
        try:
            uvicorn.run(runtime.app, host="127.0.0.1", port=args.port)
        finally:
            runtime.close()


if __name__ == "__main__":
    main()
