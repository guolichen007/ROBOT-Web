"""WEB-R1C 0012 scene business loop (real PostgreSQL 18.4; zero production downlink).

Saved-scene list + reopen, parking slots / corridors CRUD, route arrival per waypoint, teach from
the live vehicle pose (server-read), atomic publish with canonical digest + immutable sha256 asset,
DB-level immutability of published versions, SIM-only load request, and the latest-load receipt
persistence (robots.loaded_scene_*). Every published/loaded fact is cross-checked against the
contract functions (services/contracts_v21.py) instead of re-implementing them.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import get_settings
from app.core.events import get_redis
from app.core.security import hash_password
from app.db.models import (
    Asset,
    AuditLog,
    Command,
    Map,
    MapVersion,
    OutboxEvent,
    Robot,
    SceneLoadReceiptRow,
    SceneLoadRequest,
    Site,
    User,
)
from app.db.session import SessionLocal
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError

from services import contracts_v21 as c

YAML = (
    "image: sim_r002_gz.pgm\nresolution: 0.05\norigin: [-10.0, -5.0, 0.0]\n"
    "negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n"
)
PGM = b"P5\n40 30\n255\n" + bytes([254]) * (40 * 30)
CHECKSUM = "sim-gz-r002-v1"
SIM_ID = "firebot-sim-r002-01"
SITES: list[str] = []


def _downlink() -> tuple[int, int]:
    with SessionLocal() as db:
        return (
            db.scalar(select(func.count()).select_from(Command)) or 0,
            db.scalar(select(func.count()).select_from(OutboxEvent)) or 0,
        )


@pytest.fixture(autouse=True)
def _baseline():
    get_redis().flushdb()
    with SessionLocal.begin() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        admin.password_hash = hash_password(get_settings().effective_admin_password)
        admin.must_change_password = False
        admin.status = "ACTIVE"
    before = _downlink()
    yield
    assert _downlink() == before, "scene work must never create Command/Outbox rows"
    # whole-version delete (incl. PUBLISHED) is permitted by the immutability triggers: the
    # cascade into child rows runs after the parent row is gone — this teardown proves it
    with SessionLocal.begin() as db:
        db.execute(
            text(
                "UPDATE robots SET latest_scene_load_id=NULL, loaded_scene_version_id=NULL,"
                " loaded_scene_digest=NULL, loaded_scene_boot_id=NULL"
            )
        )
        db.execute(delete(SceneLoadReceiptRow))
        db.execute(delete(SceneLoadRequest))
        if SITES:
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
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _scene(client, auth, version="sim-1") -> dict:
    r = client.post(
        "/api/v1/sites", json={"code": f"pl_{uuid4().hex[:8]}", "name": "发布加载"}, headers=auth
    )
    SITES.append(r.json()["id"])
    r = client.post(
        "/api/v1/scenes/import",
        data={"site_id": SITES[-1], "map_code": "SIM_GZ_R002", "version": version},
        files={
            "yaml_file": ("sim_r002_gz.yaml", YAML.encode(), "text/yaml"),
            "image_file": ("sim_r002_gz.pgm", PGM, "application/octet-stream"),
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()


def _pt(code, kind="INSPECTION", x=0.0, y=0.0, yaw=0.0, **extra):
    return {
        "point_id": str(uuid4()),
        "code": code,
        "kind": kind,
        "x": x,
        "y": y,
        "yaw": yaw,
        **extra,
    }


def _slot(code, cx=1.0, cy=1.0):
    return {
        "slot_id": str(uuid4()),
        "code": code,
        "polygon": [
            {"x": cx - 1, "y": cy - 1},
            {"x": cx + 1, "y": cy - 1},
            {"x": cx + 1, "y": cy + 1},
        ],
        "center_x": cx,
        "center_y": cy,
        "center_yaw": 0.5,
        "enabled": True,
    }


def _save(client, auth, scene, points, routes, slots=(), corridors=()):
    return client.put(
        f"/api/v1/scenes/{scene['scene_id']}",
        json={
            "expected_revision": scene["revision"],
            "points": points,
            "routes": routes,
            "parking_slots": list(slots),
            "corridors": list(corridors),
        },
        headers=auth,
    )


def _full_scene(client, auth) -> tuple[dict, dict]:
    """A complete draft: slot, corridor, HOME + 4 points, one route with an intermediate STOP."""
    s = _scene(client, auth)
    s = client.put(
        f"/api/v1/scenes/{s['scene_id']}/nav-map-checksum",
        json={"expected_revision": s["revision"], "map_checksum": CHECKSUM},
        headers=auth,
    ).json()
    slot = _slot("S-01")
    corridor = {
        "corridor_id": str(uuid4()),
        "code": "LANE-1",
        "kind": "RIGHT_LANE",
        "centerline": [{"x": 0, "y": 0}, {"x": 5, "y": 0}],
        "width_m": 1.2,
    }
    home = _pt("HOME", "RETURN_HOME")
    p = [_pt(f"P{i}", x=float(i), slot_id=slot["slot_id"] if i == 2 else None) for i in range(1, 5)]
    arrivals = ["PASS_THROUGH", "STOP", "PASS_THROUGH", "STOP"]
    route = {
        "route_id": str(uuid4()),
        "code": "R1",
        "closed": False,
        "points": [
            {"point_id": x["point_id"], "arrival": a} for x, a in zip(p, arrivals, strict=True)
        ],
        "corridor_ids": [corridor["corridor_id"]],
    }
    r = _save(client, auth, s, [home, *p], [route], [slot], [corridor])
    assert r.status_code == 200, r.text
    return r.json(), {"slot": slot, "corridor": corridor, "points": p, "route": route}


def _publish(client, auth, scene):
    return client.post(
        f"/api/v1/scenes/{scene['scene_id']}/publish",
        json={"expected_revision": scene["revision"]},
        headers=auth,
    )


# ------------------------------------------------------------------ list / reopen / CRUD
def test_saved_scene_list_and_reopen_restores_slots_corridors_and_every_arrival(client, auth):
    saved, src = _full_scene(client, auth)
    listed = client.get("/api/v1/scenes", headers=auth).json()
    row = next(x for x in listed if x["scene_id"] == saved["scene_id"])
    assert (row["status"], row["revision"], row["points"], row["routes"]) == ("DRAFT", 3, 5, 1)
    again = client.get(f"/api/v1/scenes/{saved['scene_id']}", headers=auth).json()
    assert [x["arrival"] for x in again["routes"][0]["points"]] == [
        "PASS_THROUGH",
        "STOP",
        "PASS_THROUGH",
        "STOP",
    ], "an intermediate STOP must survive save + reload"
    assert again["parking_slots"][0]["code"] == "S-01"
    assert again["parking_slots"][0]["polygon"] == src["slot"]["polygon"]
    assert again["corridors"][0]["centerline"] == src["corridor"]["centerline"]
    assert again["routes"][0]["corridor_ids"] == [src["corridor"]["corridor_id"]]
    p2 = next(p for p in again["points"] if p["code"] == "P2")
    assert p2["slot_id"] == src["slot"]["slot_id"]


def test_slot_and_corridor_crud_and_reference_rules(client, auth):
    saved, src = _full_scene(client, auth)
    # update: move the slot, widen the corridor
    slot = {**src["slot"], "center_x": 3.0}
    corridor = {**src["corridor"], "width_m": 2.0}
    pts = [{**p, "slot_id": p.get("slot_id")} for p in src["points"]]
    home = _pt("HOME", "RETURN_HOME")
    r = _save(client, auth, saved, [home, *pts], [src["route"]], [slot], [corridor])
    assert r.status_code == 200, r.text
    assert r.json()["parking_slots"][0]["center_x"] == 3.0
    assert r.json()["corridors"][0]["width_m"] == 2.0
    # delete a slot still referenced by a point -> refused, nothing written
    r2 = _save(client, auth, r.json(), [home, *pts], [src["route"]], [], [corridor])
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "POINT_SLOT_REF_MISSING"
    # delete a corridor still referenced by a route -> refused
    r3 = _save(client, auth, r.json(), [home, *pts], [src["route"]], [slot], [])
    assert r3.status_code == 422 and r3.json()["error"]["code"] == "ROUTE_CORRIDOR_REF_MISSING"
    # unlink, then delete both
    route = {**src["route"], "corridor_ids": []}
    unlinked = [{**p, "slot_id": None} for p in pts]
    r4 = _save(client, auth, r.json(), [home, *unlinked], [route], [], [])
    assert r4.status_code == 200, r4.text
    assert r4.json()["parking_slots"] == [] and r4.json()["corridors"] == []


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing_arrival", "INVALID_REQUEST"),
        ("final_pass_through", "ROUTE_FINAL_WAYPOINT_NOT_STOP"),
        ("zero_length_corridor", "CORRIDOR_ZERO_LENGTH_SEGMENT"),
        ("slot_from_other_version", "POINT_SLOT_REF_MISSING"),
    ],
)
def test_route_and_geometry_rules(client, auth, mutation, code):
    s = _scene(client, auth)
    a, b = _pt("A"), _pt("B", x=1)
    route = {
        "route_id": str(uuid4()),
        "code": "R",
        "points": [
            {"point_id": a["point_id"], "arrival": "PASS_THROUGH"},
            {"point_id": b["point_id"], "arrival": "STOP"},
        ],
    }
    corridors: list[dict] = []
    if mutation == "missing_arrival":
        del route["points"][0]["arrival"]
    elif mutation == "final_pass_through":
        route["points"][1]["arrival"] = "PASS_THROUGH"
    elif mutation == "zero_length_corridor":
        corridors = [
            {
                "corridor_id": str(uuid4()),
                "code": "Z",
                "centerline": [{"x": 1, "y": 1}, {"x": 1, "y": 1}],
                "width_m": 1,
            }
        ]
    elif mutation == "slot_from_other_version":
        other, src = _full_scene(client, auth)
        a = {**a, "slot_id": src["slot"]["slot_id"]}
    r = _save(client, auth, s, [a, b], [route], (), corridors)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code


# ------------------------------------------------------------------ teach from vehicle pose
def _set_latest(vehicle: str, **over):
    base = {
        "vehicle_id": vehicle,
        "x": 2.25,
        "y": -1.5,
        "theta": 1.25,
        "map_code": "SIM_GZ_R002",
        "map_version": "sim-1",
        "map_checksum": CHECKSUM,
        "localization_status": "OK",
        "boot_id": str(uuid4()),
        "server_received_at": datetime.now(UTC).isoformat(),
    }
    get_redis().set(f"robot:{vehicle}:latest", json.dumps({**base, **over}))


@pytest.mark.parametrize(
    ("over", "online", "code"),
    [
        ({}, "ONLINE", None),
        (
            {"server_received_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()},
            "ONLINE",
            "POSE_STALE",
        ),
        ({"localization_status": "LOST"}, "ONLINE", "POSE_NOT_LIVE"),
        ({"map_version": "other-9"}, "ONLINE", "POSE_MAP_MISMATCH"),
        ({}, "OFFLINE", "POSE_NOT_LIVE"),
    ],
)
def test_teach_uses_only_a_live_server_pose_on_the_same_nav_map(client, auth, over, online, code):
    s = _scene(client, auth)
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        previous, robot.online_state = robot.online_state, online
    try:
        _set_latest("R002", **over)
        body = {
            "expected_revision": s["revision"],
            "vehicle_id": "R002",
            "point_id": str(uuid4()),
            "code": "T1",
            "kind": "INSPECTION",
        }
        r = client.post(f"/api/v1/scenes/{s['scene_id']}/teach", json=body, headers=auth)
        if code is None:
            assert r.status_code == 200, r.text
            p = next(p for p in r.json()["points"] if p["code"] == "T1")
            assert (p["x"], p["y"], p["yaw"], p["taught_from"]) == (
                2.25,
                -1.5,
                1.25,
                "VEHICLE_POSE",
            )
            # a client cannot forge provenance: re-saving keeps VEHICLE_POSE only for this point,
            # and a fresh point claiming VEHICLE_POSE is downgraded to MANUAL_INPUT
            fake = _pt("F", taught_from="VEHICLE_POSE")
            again = _save(client, auth, r.json(), [*r.json()["points"], fake], [])
            got = {p["code"]: p["taught_from"] for p in again.json()["points"]}
            assert got == {"T1": "VEHICLE_POSE", "F": "MANUAL_INPUT"}
        else:
            assert r.status_code == 409 and r.json()["error"]["code"] == code, r.text
            assert (
                client.get(f"/api/v1/scenes/{s['scene_id']}", headers=auth).json()["points"] == []
            )
    finally:
        with SessionLocal.begin() as db:
            db.scalar(select(Robot).where(Robot.vehicle_id == "R002")).online_state = previous


# ------------------------------------------------------------------ atomic publish / immutability
def test_atomic_publish_digest_asset_and_contract_validity(client, auth):
    saved, _ = _full_scene(client, auth)
    r = _publish(client, auth, saved)
    assert r.status_code == 200, r.text
    pub = r.json()
    assert pub["status"] == "PUBLISHED" and len(pub["scene_digest"]) == 64
    assert pub["traversability"] == "PENDING_VERIFICATION", "never claimed without evidence"
    m = client.get(pub["manifest_url"], headers=auth).json()
    c.validate("SceneManifest", m["manifest"])  # schema + references + digest rule
    assert m["manifest"]["digest"] == pub["scene_digest"] == c.scene_digest(m["manifest"])
    assert m["route_arrivals"][m["manifest"]["routes"][0]["route_id"]] == [
        "PASS_THROUGH",
        "STOP",
        "PASS_THROUGH",
        "STOP",
    ]
    with SessionLocal() as db:
        v = db.get(MapVersion, saved["scene_id"])
        asset = db.get(Asset, v.scene_asset_id)
        raw = Path(get_settings().asset_root, asset.object_name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == asset.sha256 == m["asset_sha256"]


def test_publish_requires_nav_map_checksum_and_revision_and_is_single_shot(client, auth):
    s = _scene(client, auth)
    r = _publish(client, auth, s)
    assert r.status_code == 409 and r.json()["error"]["code"] == "NAV_MAP_CHECKSUM_MISSING"
    saved, _ = _full_scene(client, auth)
    stale = {**saved, "revision": saved["revision"] - 1}
    assert _publish(client, auth, stale).json()["error"]["code"] == "REVISION_CONFLICT"
    results: list[int] = []

    def go():
        with TestClient(app) as cl:
            results.append(_publish(cl, auth, saved).status_code)

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409, 409], results
    with SessionLocal() as db:
        assets = db.scalar(
            select(func.count())
            .select_from(Asset)
            .where(Asset.original_filename.like(f"scene-{saved['scene_id']}-%"))
        )
    assert assets == 1, "losing publishers must not leave scene assets behind"


def test_invalid_scene_is_not_published(client, auth):
    s = _scene(client, auth)
    s = client.put(
        f"/api/v1/scenes/{s['scene_id']}/nav-map-checksum",
        json={"expected_revision": s["revision"], "map_checksum": CHECKSUM},
        headers=auth,
    ).json()
    s = _save(client, auth, s, [_pt("ONLY")], []).json()  # no RETURN_HOME point
    r = _publish(client, auth, s)
    assert r.status_code == 422 and "RETURN_HOME_POINT_MISSING" in r.json()["error"]["message"]
    assert client.get(f"/api/v1/scenes/{s['scene_id']}", headers=auth).json()["status"] == "DRAFT"


def test_published_version_is_immutable_at_the_database_level(client, auth):
    saved, _ = _full_scene(client, auth)
    pub = _publish(client, auth, saved).json()
    sid = pub["scene_id"]
    for sql in (
        "UPDATE scene_points SET x = x + 1 WHERE map_version_id = :v",
        "DELETE FROM scene_route_points WHERE map_version_id = :v",
        "UPDATE scene_route_points SET arrival = 'PASS_THROUGH' WHERE map_version_id = :v",
        "UPDATE scene_parking_slots SET center_x = 9 WHERE map_version_id = :v",
        "UPDATE scene_corridors SET width_m = 3 WHERE map_version_id = :v",
        "UPDATE map_versions SET scene_digest = repeat('0', 64) WHERE id = :v",
        "UPDATE map_versions SET revision = revision + 1 WHERE id = :v",
        "UPDATE map_versions SET status = 'DRAFT' WHERE id = :v",
        "UPDATE map_versions SET traversability = 'VERIFIED' WHERE id = :v",
    ):
        with pytest.raises(DBAPIError) as err, SessionLocal.begin() as db:
            db.execute(text(sql), {"v": sid})
        assert "SCENE_PUBLISHED_IMMUTABLE" in str(err.value), sql
    with pytest.raises(DBAPIError), SessionLocal.begin() as db:
        db.execute(
            text(
                "INSERT INTO scene_points (id, map_version_id, point_uid, code, kind, x, y, yaw,"
                " taught_from) VALUES (:i, :v, :u, 'X', 'TRANSIT', 0, 0, 0, 'MANUAL_INPUT')"
            ),
            {"i": str(uuid4()), "v": sid, "u": str(uuid4())},
        )
    # archiving (status only) is the one permitted change
    with SessionLocal.begin() as db:
        db.execute(text("UPDATE map_versions SET status='ARCHIVED' WHERE id=:v"), {"v": sid})
    # API refuses edits too
    assert _save(client, auth, pub, [], []).json()["error"]["code"] == "SCENE_NOT_DRAFT"


def test_tampered_asset_is_never_served(client, auth):
    saved, _ = _full_scene(client, auth)
    pub = _publish(client, auth, saved).json()
    with SessionLocal() as db:
        asset = db.get(Asset, db.get(MapVersion, pub["scene_id"]).scene_asset_id)
    path = Path(get_settings().asset_root, asset.object_name)
    path.write_bytes(path.read_bytes().replace(b"PASS_THROUGH", b"STOP_________", 1))
    r = client.get(pub["manifest_url"], headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "SCENE_ASSET_TAMPERED"


def test_publishing_does_not_change_the_operating_map(client, auth):
    before = client.get("/api/v1/monitor/snapshot", headers=auth).json()["map_version"]
    saved, _ = _full_scene(client, auth)
    _publish(client, auth, saved)
    after = client.get("/api/v1/monitor/snapshot", headers=auth).json()["map_version"]
    assert (before or {}).get("id") == (after or {}).get("id"), "publish != load != operate"


# ------------------------------------------------------------------ load request (SIM only)
def test_load_is_refused_for_real_vehicles_and_outside_sim(client, auth, monkeypatch):
    saved, _ = _full_scene(client, auth)
    pub = _publish(client, auth, saved).json()
    r = client.post(
        f"/api/v1/scenes/{pub['scene_id']}/load", json={"vehicle_id": "R002"}, headers=auth
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "SCENE_LOAD_SIM_ONLY"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(SceneLoadRequest)) == 0
