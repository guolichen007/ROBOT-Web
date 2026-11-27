"""WEB-R1B 0011 scene persistence counterexamples (real PostgreSQL; zero downlink).

Covers batch 1: import -> server validation -> draft save -> reload restores -> point/route edit
and delete -> concurrency conflict. Cross-version integrity is proven at the DB level by raw SQL
that bypasses the API (composite FKs), not only by application checks.
"""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest
from app.core.config import get_settings
from app.core.events import get_redis
from app.core.security import hash_password
from app.db.models import AuditLog, Command, Map, MapVersion, OutboxEvent, Site, User
from app.db.session import SessionLocal
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

YAML = (
    "image: sim_r002_gz.pgm\nresolution: 0.05\norigin: [-10.0, -5.0, 0.0]\n"
    "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
)
PGM = b"P5\n# firebot SIM synthetic\n40 30\n255\n" + bytes([254]) * (40 * 30)
CREATED: list[str] = []  # site ids


def downlink_counts() -> tuple[int, int]:
    with SessionLocal() as db:
        return (
            db.scalar(select(func.count()).select_from(Command)) or 0,
            db.scalar(select(func.count()).select_from(OutboxEvent)) or 0,
        )


@pytest.fixture(autouse=True)
def zero_downlink_and_cleanup():
    before = downlink_counts()
    yield
    assert downlink_counts() == before, "scene editing must never create Command/Outbox rows"
    if CREATED:
        with SessionLocal.begin() as db:
            map_ids = db.scalars(select(Map.id).where(Map.site_id.in_(CREATED))).all()
            if map_ids:
                db.execute(delete(MapVersion).where(MapVersion.map_id.in_(map_ids)))
                db.execute(delete(Map).where(Map.id.in_(map_ids)))
            db.execute(delete(AuditLog).where(AuditLog.resource_id.in_(CREATED)))
            db.execute(delete(Site).where(Site.id.in_(CREATED)))
        CREATED.clear()


@pytest.fixture(autouse=True)
def baseline() -> None:
    get_redis().flushdb()
    with SessionLocal.begin() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin
        admin.password_hash = hash_password(get_settings().effective_admin_password)
        admin.must_change_password = False
        admin.status = "ACTIVE"


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
        "/api/v1/sites", json={"code": f"sv_{uuid4().hex[:8]}", "name": "场景持久化"}, headers=auth
    )
    assert r.status_code == 201, r.text
    CREATED.append(r.json()["id"])
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


def _pt(code, kind="INSPECTION", x=0.0, y=0.0, yaw=0.0):
    return {"point_id": str(uuid4()), "code": code, "kind": kind, "x": x, "y": y, "yaw": yaw}


def _scene(client, auth) -> dict:
    r = _import(client, auth, _site(client, auth))
    assert r.status_code == 201, r.text
    return r.json()


def _save(client, auth, scene, points, routes, rev=None):
    return client.put(
        f"/api/v1/scenes/{scene['scene_id']}",
        json={"expected_revision": rev or scene["revision"], "points": points, "routes": routes},
        headers=auth,
    )


def test_import_validates_server_side_and_creates_v2_draft(client, auth) -> None:
    scene = _scene(client, auth)
    assert scene["storage_version"] == 2 and scene["status"] == "DRAFT" and scene["revision"] == 1
    nav = scene["nav_map"]
    assert (nav["width_px"], nav["height_px"], nav["resolution_m_per_px"]) == (40, 30, 0.05)
    assert nav["origin"] == {"x": -10.0, "y": -5.0, "yaw": 0.0}
    assert len(nav["image_sha256"]) == 64 and nav["map_checksum"] is None
    assert scene["traversability"] == "PENDING_VERIFICATION"
    assert client.get(nav["image_url"], headers=auth).content == PGM


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        (
            {"yaml": YAML.replace("sim_r002_gz.pgm", "../sim_r002_gz.pgm")},
            "YAML_IMAGE_PATH_FORBIDDEN",
        ),
        ({"yaml": YAML.replace("0.05", ".nan")}, "YAML_NOT_A_FINITE_NUMBER"),
        ({"yaml": YAML + "evil: 1\n"}, "YAML_UNKNOWN_KEY"),
        ({"yaml": YAML.replace("0.196", "0.9")}, "YAML_THRESHOLDS_INVALID"),
        ({"image": PGM[:-1]}, "PGM_DATA_TRUNCATED"),
        ({"image_name": "other.pgm"}, "YAML_IMAGE_NAME_MISMATCH"),
        ({"image": b"P5 9000 10 255\n"}, "IMAGE_TOO_MANY_PIXELS"),
    ],
)
def test_import_rejections(client, auth, kwargs, code) -> None:
    r = _import(client, auth, _site(client, auth), **kwargs)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code


def test_import_respects_api_upload_limit(client, auth, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "upload_max_bytes", 1000)
    r = _import(client, auth, _site(client, auth))
    assert r.status_code == 422 and r.json()["error"]["code"] == "IMAGE_TOO_LARGE"


def test_save_then_reload_restores_exactly(client, auth) -> None:
    scene = _scene(client, auth)
    home, a, b = _pt("HOME", "RETURN_HOME"), _pt("I1", x=1.25, y=-0.5, yaw=1.5708), _pt("I2", x=3)
    route = {
        "route_id": str(uuid4()),
        "code": "R1",
        "points": [
            {"point_id": a["point_id"]},
            {"point_id": b["point_id"], "arrival": "STOP"},
        ],
    }
    r = _save(client, auth, scene, [home, a, b], [route])
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2
    reloaded = client.get(f"/api/v1/scenes/{scene['scene_id']}", headers=auth).json()
    assert reloaded["revision"] == 2
    got = {p["code"]: (p["x"], p["y"], p["yaw"], p["kind"]) for p in reloaded["points"]}
    assert got == {
        "HOME": (0.0, 0.0, 0.0, "RETURN_HOME"),
        "I1": (1.25, -0.5, 1.5708, "INSPECTION"),
        "I2": (3.0, 0.0, 0.0, "INSPECTION"),
    }
    assert reloaded["routes"][0]["points"] == [
        {"point_id": a["point_id"], "arrival": "PASS_THROUGH"},
        {"point_id": b["point_id"], "arrival": "STOP"},
    ]


def test_edit_and_delete_points_and_routes(client, auth) -> None:
    scene = _scene(client, auth)
    a, b, c = _pt("I1"), _pt("I2", x=1), _pt("I3", x=2)
    route = {
        "route_id": str(uuid4()),
        "code": "R1",
        "points": [{"point_id": a["point_id"]}, {"point_id": b["point_id"]}],
    }
    s2 = _save(client, auth, scene, [a, b, c], [route]).json()
    moved = {**a, "x": 9.5, "yaw": -1.0}
    s3 = _save(client, auth, s2, [moved, b, c], [route]).json()
    assert next(p for p in s3["points"] if p["code"] == "I1")["x"] == 9.5
    # referenced point cannot be deleted
    r = client.request(
        "DELETE",
        f"/api/v1/scenes/{scene['scene_id']}/points/{a['point_id']}",
        json={"expected_revision": s3["revision"]},
        headers=auth,
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "POINT_REFERENCED_BY_ROUTE"
    # unreferenced point deleted
    r = client.request(
        "DELETE",
        f"/api/v1/scenes/{scene['scene_id']}/points/{c['point_id']}",
        json={"expected_revision": s3["revision"]},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    s4 = r.json()
    assert {p["code"] for p in s4["points"]} == {"I1", "I2"} and s4["revision"] == s3[
        "revision"
    ] + 1
    # route deleted, then its former point becomes deletable
    r = client.request(
        "DELETE",
        f"/api/v1/scenes/{scene['scene_id']}/routes/{route['route_id']}",
        json={"expected_revision": s4["revision"]},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["routes"] == []
    r = client.request(
        "DELETE",
        f"/api/v1/scenes/{scene['scene_id']}/points/{a['point_id']}",
        json={"expected_revision": s4["revision"] + 1},
        headers=auth,
    )
    assert r.status_code == 200, r.text


def test_save_rejects_dangling_route_reference(client, auth) -> None:
    scene = _scene(client, auth)
    a = _pt("I1")
    route = {
        "route_id": str(uuid4()),
        "code": "R1",
        "points": [{"point_id": a["point_id"]}, {"point_id": str(uuid4())}],
    }
    r = _save(client, auth, scene, [a], [route])
    assert r.status_code == 422 and r.json()["error"]["code"] == "ROUTE_POINT_REF_MISSING"
    assert client.get(f"/api/v1/scenes/{scene['scene_id']}", headers=auth).json()["revision"] == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"x": float("nan")},
        {"yaw": 4.0},
        {"x": 1e9},
        {"kind": "ANYWHERE"},
        {"parking_slot_code": "S-01"},
    ],
)
def test_save_rejects_invalid_point(client, auth, bad) -> None:
    scene = _scene(client, auth)
    body = {"expected_revision": 1, "points": [{**_pt("I1"), **bad}], "routes": []}
    import json

    r = client.put(
        f"/api/v1/scenes/{scene['scene_id']}",
        content=json.dumps(body, allow_nan=True),
        headers={**auth, "Content-Type": "application/json"},
    )
    assert r.status_code == 422, r.text


def test_stale_revision_conflict_never_silently_overwrites(client, auth) -> None:
    scene = _scene(client, auth)
    first = _save(client, auth, scene, [_pt("MINE")], [])
    assert first.status_code == 200
    stale = _save(client, auth, scene, [_pt("THEIRS")], [], rev=1)
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "REVISION_CONFLICT" and err["details"]["current_revision"] == 2
    kept = client.get(f"/api/v1/scenes/{scene['scene_id']}", headers=auth).json()
    assert [p["code"] for p in kept["points"]] == ["MINE"]


def test_concurrent_saves_one_wins_one_conflicts(client, auth) -> None:
    scene = _scene(client, auth)
    token = auth["Authorization"]
    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker(code: str) -> None:
        with TestClient(app) as c:
            barrier.wait()
            r = c.put(
                f"/api/v1/scenes/{scene['scene_id']}",
                json={"expected_revision": 1, "points": [_pt(code)], "routes": []},
                headers={"Authorization": token},
            )
            results.append(r.status_code)

    threads = [threading.Thread(target=worker, args=(f"P{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409]
    final = client.get(f"/api/v1/scenes/{scene['scene_id']}", headers=auth).json()
    assert final["revision"] == 2 and len(final["points"]) == 1


def test_db_composite_fk_rejects_cross_version_reference(client, auth) -> None:
    """Bypass the API entirely: a route point may not reference a point of another version."""
    s1, s2 = _scene(client, auth), None
    s2 = _import(client, auth, _site(client, auth), version="sim-2").json()
    a1, a2 = _pt("A1"), _pt("A2")
    _save(client, auth, s1, [a1, _pt("B1")], [])
    _save(client, auth, s2, [a2], [])
    with pytest.raises(IntegrityError), SessionLocal.begin() as db:
        route_uid = str(uuid4())
        db.execute(
            text(
                "INSERT INTO scene_routes (id, map_version_id, route_uid, code, closed) "
                "VALUES (:i,:v,:r,'X',false)"
            ),
            {"i": str(uuid4()), "v": s1["scene_id"], "r": route_uid},
        )
        db.execute(
            text(
                "INSERT INTO scene_route_points "
                "(map_version_id, route_uid, seq, point_uid, arrival) VALUES (:v,:r,0,:p,'STOP')"
            ),
            {"v": s1["scene_id"], "r": route_uid, "p": a2["point_id"]},  # point of version 2
        )


def test_db_rejects_deleting_a_referenced_point_directly(client, auth) -> None:
    scene = _scene(client, auth)
    a, b = _pt("I1"), _pt("I2")
    _save(
        client,
        auth,
        scene,
        [a, b],
        [
            {
                "route_id": str(uuid4()),
                "code": "R",
                "points": [{"point_id": a["point_id"]}, {"point_id": b["point_id"]}],
            }
        ],
    )
    with pytest.raises(IntegrityError), SessionLocal.begin() as db:
        db.execute(
            text("DELETE FROM scene_points WHERE map_version_id=:v AND point_uid=:p"),
            {"v": scene["scene_id"], "p": a["point_id"]},
        )


def test_published_v2_scene_is_immutable(client, auth) -> None:
    scene = _scene(client, auth)
    with SessionLocal.begin() as db:
        db.get(MapVersion, scene["scene_id"]).status = "PUBLISHED"
    r = _save(client, auth, scene, [_pt("I1")], [])
    assert r.status_code == 409 and r.json()["error"]["code"] == "SCENE_NOT_DRAFT"


def test_legacy_writers_refuse_v2_scene_and_v2_api_refuses_legacy(client, auth) -> None:
    scene = _scene(client, auth)
    r = client.post(
        "/api/v1/parking-slots",
        json={
            "map_version_id": scene["scene_id"],
            "code": "S-01",
            "polygon_json": {"points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]},
            "center_pose_json": {"x": 0.5, "y": 0.5, "theta": 0},
        },
        headers=auth,
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "LEGACY_WRITE_ON_V21_SCENE"
    with SessionLocal() as db:
        legacy_id = db.scalar(select(MapVersion.id).where(MapVersion.storage_version == 1))
    assert legacy_id, "seeded legacy version exists"
    r = client.get(f"/api/v1/scenes/{legacy_id}", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "LEGACY_STORAGE_VERSION"


def test_viewer_cannot_write(client, auth) -> None:
    scene = _scene(client, auth)
    r = client.put(
        f"/api/v1/scenes/{scene['scene_id']}",
        json={"expected_revision": 1, "points": [], "routes": []},
    )
    assert r.status_code in (401, 403)


def test_whole_version_delete_cascades_with_routes(client, auth) -> None:
    """Regression for the NO ACTION ordering defect: deleting a version that has routes works."""
    scene = _scene(client, auth)
    a, b = _pt("I1"), _pt("I2")
    pts = [{"point_id": a["point_id"]}, {"point_id": b["point_id"]}]
    route = {"route_id": str(uuid4()), "code": "R", "points": pts}
    assert _save(client, auth, scene, [a, b], [route]).status_code == 200
    with SessionLocal.begin() as db:
        db.execute(delete(MapVersion).where(MapVersion.id == scene["scene_id"]))
    with SessionLocal() as db:
        left = db.scalar(
            text("SELECT count(*) FROM scene_points WHERE map_version_id=:v"),
            {"v": scene["scene_id"]},
        )
    assert left == 0
