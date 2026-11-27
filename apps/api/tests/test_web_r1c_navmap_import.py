"""WEB-R1C R1C-3: map-file counterexamples + import transaction / orphan-asset guarantees.

Real PostgreSQL. Each counterexample below was ACCEPTED by the R1B header-only validator
(independent reproduction: FALSE_ACCEPTS 3/3); every import failure must leave no DB row and
no file behind, and a duplicate import must be refused before anything is written.
"""

from __future__ import annotations

import io
import struct
import threading
import zlib
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import get_settings
from app.core.events import get_redis
from app.core.security import hash_password
from app.db.models import Asset, AuditLog, Map, MapVersion, Site, User
from app.db.session import SessionLocal
from app.main import app
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import delete, func, select

YAML = (
    "image: sim_r002_gz.pgm\nresolution: 0.05\norigin: [-10.0, -5.0, 0.0]\n"
    "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
)
PGM = b"P5\n# firebot SIM synthetic\n40 30\n255\n" + bytes([254]) * (40 * 30)
SITES: list[str] = []


@pytest.fixture(autouse=True)
def _baseline():
    get_redis().flushdb()
    with SessionLocal.begin() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        admin.password_hash = hash_password(get_settings().effective_admin_password)
        admin.must_change_password = False
        admin.status = "ACTIVE"
    yield
    if SITES:
        with SessionLocal.begin() as db:
            map_ids = db.scalars(select(Map.id).where(Map.site_id.in_(SITES))).all()
            if map_ids:
                db.execute(delete(MapVersion).where(MapVersion.map_id.in_(map_ids)))
                db.execute(delete(Map).where(Map.id.in_(map_ids)))
            db.execute(delete(AuditLog).where(AuditLog.resource_id.in_(SITES)))
            db.execute(delete(Site).where(Site.id.in_(SITES)))
        SITES.clear()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as value:
        yield value


@pytest.fixture
def auth(client: TestClient) -> dict[str, str]:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": get_settings().effective_admin_password},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _site(client, auth) -> str:
    r = client.post(
        "/api/v1/sites", json={"code": f"nm_{uuid4().hex[:8]}", "name": "导入校验"}, headers=auth
    )
    assert r.status_code == 201, r.text
    SITES.append(r.json()["id"])
    return r.json()["id"]


def _import(
    client, auth, site_id, version="sim-1", yaml=YAML, image=PGM, image_name="sim_r002_gz.pgm"
):
    return client.post(
        "/api/v1/scenes/import",
        data={"site_id": site_id, "map_code": "SIM_GZ_R002", "version": version},
        files={
            "yaml_file": ("sim_r002_gz.yaml", yaml.encode(), "text/yaml"),
            "image_file": (image_name, image, "application/octet-stream"),
        },
        headers=auth,
    )


def _png_header_only(w: int = 64, h: int = 64) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    chunk = struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return b"\x89PNG\r\n\x1a\n" + chunk


def _png(w: int = 40, h: int = 30) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (w, h), 254).save(buf, "PNG")
    return buf.getvalue()


def _corrupt_idat(data: bytes) -> bytes:
    """Flip one compressed byte inside IDAT; the file still ends with a valid IEND chunk."""
    i = data.index(b"IDAT") + 6
    return data[:i] + bytes([data[i] ^ 0xFF]) + data[i + 1 :]


PNG_YAML = YAML.replace("sim_r002_gz.pgm", "sim_r002_gz.png")


def _files() -> set[str]:
    root = get_settings().asset_root.resolve()
    return {p.name for p in root.glob("*")} if root.exists() else set()


def _counts() -> tuple[int, int, int]:
    with SessionLocal() as db:
        return (
            db.scalar(select(func.count()).select_from(MapVersion)) or 0,
            db.scalar(select(func.count()).select_from(Asset)) or 0,
            db.scalar(select(func.count()).select_from(Map)) or 0,
        )


@pytest.mark.parametrize(
    ("name", "kwargs", "code"),
    [
        (
            "png_header_only",
            {"yaml": PNG_YAML, "image": _png_header_only(), "image_name": "sim_r002_gz.png"},
            "PNG_TRUNCATED",
        ),
        ("p2_pgm_no_pixels", {"image": b"P2\n4 3\n255\n"}, "PGM_DATA_TRUNCATED"),
        (
            "p5_pixel_over_maxval",
            {"image": b"P5\n2 2\n100\n" + bytes([0, 50, 100, 200])},
            "PGM_PIXEL_OVER_MAXVAL",
        ),
        (
            "png_corrupt_idat",
            {
                "yaml": PNG_YAML,
                "image": (
                    lambda d: d[: d.index(b"IDAT") + 6]
                    + bytes([d[d.index(b"IDAT") + 6] ^ 0xFF])
                    + d[d.index(b"IDAT") + 7 :]
                )(_png()),
                "image_name": "sim_r002_gz.png",
            },
            "PNG_DECODE_FAILED",
        ),
        ("p5_trailing_bytes", {"image": b"P5\n2 2\n255\n" + bytes(5)}, "PGM_TRAILING_DATA"),
        ("p2_pixel_not_a_number", {"image": b"P2\n1 1\n255\nx\n"}, "PGM_PIXEL_INVALID"),
    ],
)
def test_r1c_map_file_counterexamples_rejected_without_side_effects(
    client, auth, name, kwargs, code
) -> None:
    site = _site(client, auth)
    before_files, before_rows = _files(), _counts()
    r = _import(client, auth, site, **kwargs)
    assert r.status_code == 422, (name, r.text)
    assert r.json()["error"]["code"] == code, name
    assert _files() == before_files, f"{name}: a rejected import left a file behind"
    assert _counts() == before_rows, f"{name}: a rejected import left DB rows behind"


def test_r1c_valid_png_and_ascii_pgm_still_import(client, auth) -> None:
    site = _site(client, auth)
    r = _import(client, auth, site, yaml=PNG_YAML, image=_png(), image_name="sim_r002_gz.png")
    assert r.status_code == 201, r.text
    assert (r.json()["nav_map"]["width_px"], r.json()["nav_map"]["height_px"]) == (40, 30)
    p2 = b"P2\n# ascii\n3 2\n255\n0 205 254\n255 0 205\n"
    r2 = _import(client, auth, site, version="sim-2", image=p2)
    assert r2.status_code == 201, r2.text


def test_r1c_duplicate_import_refused_before_any_write(client, auth) -> None:
    site = _site(client, auth)
    assert _import(client, auth, site).status_code == 201
    before_files, before_rows = _files(), _counts()
    r = _import(client, auth, site)  # same site / map / version
    assert r.status_code == 409 and r.json()["error"]["code"] == "SCENE_VERSION_EXISTS"
    assert _files() == before_files and _counts() == before_rows


def test_r1c_file_write_failure_leaves_no_row_and_no_partial_file(
    client, auth, monkeypatch
) -> None:
    import os

    site = _site(client, auth)
    real_replace, calls = os.replace, {"n": 0}

    def failing_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # image written, YAML rename fails
            raise OSError("disk full (injected)")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    before_files, before_rows = _files(), _counts()
    r = _import(client, auth, site)
    assert r.status_code == 500 and r.json()["error"]["code"] == "ASSET_WRITE_FAILED"
    after = _files()
    assert after == before_files, f"orphans: {after - before_files}"
    assert not any(n.endswith(".part") for n in after)
    assert _counts() == before_rows


def test_r1c_db_failure_after_files_written_removes_the_files(client, auth, monkeypatch) -> None:
    from app.modules.scenes import router as scenes

    site = _site(client, auth)
    real_audit = scenes.write_audit

    def exploding_audit(*args, **kwargs):
        raise RuntimeError("db failure after asset write (injected)")

    monkeypatch.setattr(scenes, "write_audit", exploding_audit)
    before_files, before_rows = _files(), _counts()
    r = _import(client, auth, site)  # the API turns the unhandled error into a 500
    assert r.status_code == 500
    monkeypatch.setattr(scenes, "write_audit", real_audit)
    assert _files() == before_files, "files of a rolled-back import must be removed"
    assert _counts() == before_rows


def test_r1c_orphan_free_after_concurrent_duplicate_imports(client, auth) -> None:
    site = _site(client, auth)
    before_files = _files()
    results: list[int] = []

    def go() -> None:
        with TestClient(app) as c:
            results.append(_import(c, auth, site, version="race-1").status_code)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(201) == 1 and all(s in (201, 409) for s in results), results
    with SessionLocal() as db:
        v = db.scalar(select(MapVersion).where(MapVersion.version == "race-1"))
        referenced = {
            db.get(Asset, v.nav_image_asset_id).object_name,
            db.get(Asset, v.nav_yaml_asset_id).object_name,
        }
    new_files = _files() - before_files
    assert new_files == referenced, f"orphan files: {new_files - referenced}"
    assert all(Path(get_settings().asset_root, n).exists() for n in referenced)
