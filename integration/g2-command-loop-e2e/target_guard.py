"""G2 E2E 测试目标防误连（任何 DB 写入 / Redis flushdb 之前必须通过）。

判定只看“连接目标身份”，不信任任何名字里带 test 的约定：
- APP_ENV=test 且 REAL_E2E=1；
- 不得使用 *_FILE 形式的连接串（生产 server profile 的 secrets 注入方式）；
- 数据库：真实解析出的 host/port/dbname 必须精确命中 G2_E2E_ALLOWED_DB
  （逗号分隔 host:port/dbname；默认仅 CI/本地隔离目标 localhost|127.0.0.1:5432|15432/
  firebot_ci|firebot_e2e）；拒绝生产服务名 postgres/生产库名 firebot 与任何非回环主机；
- Redis：host 为回环、DB 号 ∈ G2_E2E_ALLOWED_REDIS_DB（默认 14,15），拒绝 DB 0；
- 连接后再次以服务端事实复核：current_database()/inet_server_port()、Redis 实际 db 号，
  并要求数据库无生产标记（存在 firebot-vehicle-02，或缺少 R001/R002 demo seed 即拒绝）。
失败一律抛 TargetGuardError——调用方直接退出，绝不继续写入。
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
DEFAULT_ALLOWED_DB = (
    "localhost:5432/firebot_ci,127.0.0.1:5432/firebot_ci,"
    "localhost:5432/firebot_e2e,127.0.0.1:5432/firebot_e2e,"
    "localhost:15432/firebot_e2e,127.0.0.1:15432/firebot_e2e"
)
DEFAULT_ALLOWED_REDIS_DB = "14,15"
PRODUCTION_DB_NAMES = {"firebot", "firebot_prod", "firebot_production"}
PRODUCTION_HOSTS = {"postgres", "redis", "mosquitto"}  # 生产 compose 服务名
PRODUCTION_VEHICLE_IDS = {"firebot-vehicle-02"}  # 生产 002 身份（测试库永不出现）


class TargetGuardError(RuntimeError):
    """测试目标身份不符：拒绝任何写入。"""


def _allowed_db() -> set[str]:
    raw = os.environ.get("G2_E2E_ALLOWED_DB", DEFAULT_ALLOWED_DB)
    return {item.strip() for item in raw.split(",") if item.strip()}


def _allowed_redis_db() -> set[int]:
    raw = os.environ.get("G2_E2E_ALLOWED_REDIS_DB", DEFAULT_ALLOWED_REDIS_DB)
    return {int(item) for item in raw.split(",") if item.strip()}


def parse_db(url: str) -> tuple[str, int, str]:
    parts = urlsplit(url.replace("postgresql+psycopg://", "postgresql://", 1))
    if parts.scheme not in {"postgresql", "postgres"}:
        raise TargetGuardError(f"unsupported database scheme {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise TargetGuardError("database host missing (unix socket targets not allowed)")
    return host, parts.port or 5432, parts.path.lstrip("/")


def parse_redis(url: str) -> tuple[str, int, int]:
    parts = urlsplit(url)
    if parts.scheme not in {"redis"}:
        raise TargetGuardError(f"unsupported redis scheme {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    path = parts.path.lstrip("/")
    if not path.isdigit():
        raise TargetGuardError("redis DB number must be explicit")
    return host, parts.port or 6379, int(path)


def check_static(env: dict | None = None) -> dict:
    """连接前检查（纯解析，零 I/O）。"""
    env = dict(os.environ if env is None else env)
    if env.get("APP_ENV") != "test" or env.get("REAL_E2E") != "1":
        raise TargetGuardError("APP_ENV=test and REAL_E2E=1 are both required")
    for key in ("DATABASE_URL_FILE", "REDIS_URL_FILE"):
        if env.get(key):
            raise TargetGuardError(f"{key} is a production secrets source; refused")
    db_url, redis_url = env.get("DATABASE_URL", ""), env.get("REDIS_URL", "")
    host, port, name = parse_db(db_url)
    if host in PRODUCTION_HOSTS or name in PRODUCTION_DB_NAMES:
        raise TargetGuardError(f"production-style database target {host}:{port}/{name}")
    if host not in LOOPBACK:
        raise TargetGuardError(f"database host {host} is not an isolated loopback target")
    if f"{host}:{port}/{name}" not in _allowed_db():
        raise TargetGuardError(f"database {host}:{port}/{name} not in G2_E2E_ALLOWED_DB")
    r_host, r_port, r_db = parse_redis(redis_url)
    if r_host in PRODUCTION_HOSTS or r_host not in LOOPBACK:
        raise TargetGuardError(f"redis host {r_host} is not an isolated loopback target")
    if r_db == 0 or r_db not in _allowed_redis_db():
        raise TargetGuardError(f"redis db {r_db} not in G2_E2E_ALLOWED_REDIS_DB")
    return {"db": (host, port, name), "redis": (r_host, r_port, r_db)}


def check_live(expected: dict) -> None:
    """连接后以服务端事实复核（仍为只读查询）。"""
    import psycopg
    import redis as redis_lib

    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url, connect_timeout=3) as conn:
        name, port = conn.execute("select current_database(), inet_server_port()").fetchone()
        if name != expected["db"][2] or (port is not None and port != expected["db"][1]):
            raise TargetGuardError(f"server reports {name}:{port}, expected {expected['db']}")
        if conn.execute("select to_regclass('public.robots')").fetchone()[0]:
            ids = {r[0] for r in conn.execute("select vehicle_id from robots").fetchall()}
            # 生产标记：真实 002 身份存在，或缺少 demo seed（server profile 禁止 demo seed）
            if PRODUCTION_VEHICLE_IDS & ids:
                raise TargetGuardError("database contains production vehicle identities")
            if not {"R001", "R002"} <= ids:
                raise TargetGuardError("database lacks test demo seed (R001/R002)")
    client = redis_lib.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=2)
    actual_db = client.connection_pool.connection_kwargs.get("db")
    client.ping()
    if actual_db != expected["redis"][2]:
        raise TargetGuardError(f"redis connected to db {actual_db}")


def assert_isolated_target() -> dict:
    expected = check_static()
    check_live(expected)
    return expected
