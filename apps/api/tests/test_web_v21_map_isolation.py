"""反证：地图子资源跨版本迁移 / PUBLISHED 冻结 / 删除被引用车位 / 点位跨版本引用。

只反证根因：原先 update 端点只校验“源版本是 DRAFT”，随后连 map_version_id 一起写回，
可把一个 DRAFT 里的点位/车位迁入任意版本（含 PUBLISHED），绕过发布冻结。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from app.core.config import get_settings
from app.core.events import get_redis
from app.core.security import hash_password
from app.db.models import AuditLog, Command, Map, MapVersion, OutboxEvent, Site, User
from app.db.session import SessionLocal
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

CREATED: list[tuple[str, str]] = []  # (site_id, map_id) created by this module


def downlink_counts() -> tuple[int, int]:
    with SessionLocal() as db:
        return (
            db.scalar(select(func.count()).select_from(Command)) or 0,
            db.scalar(select(func.count()).select_from(OutboxEvent)) or 0,
        )


@pytest.fixture(autouse=True)
def zero_downlink():
    before = downlink_counts()
    yield
    assert downlink_counts() == before, "map editing must never create Command/Outbox rows"


@pytest.fixture(autouse=True)
def cleanup_created_scene_data():
    """Leave the shared test DB exactly as found: other suites count sites/maps."""
    yield
    if not CREATED:
        return
    site_ids = [s for s, _ in CREATED]
    map_ids = [m for _, m in CREATED]
    with SessionLocal.begin() as db:
        # children (slots/points/trajectories) cascade from map_versions at DB level
        db.execute(delete(MapVersion).where(MapVersion.map_id.in_(map_ids)))
        db.execute(delete(Map).where(Map.id.in_(map_ids)))
        db.execute(delete(AuditLog).where(AuditLog.resource_id.in_(site_ids)))
        db.execute(delete(Site).where(Site.id.in_(site_ids)))
    CREATED.clear()


@pytest.fixture(autouse=True)
def baseline() -> None:
    get_redis().flushdb()
    with SessionLocal.begin() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin
        password = get_settings().effective_admin_password
        admin.password_hash = hash_password(password)
        admin.must_change_password = False
        admin.status = "ACTIVE"


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as value:
        yield value


@pytest.fixture
def auth(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": get_settings().effective_admin_password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _site_map(client: TestClient, auth: dict) -> tuple[str, str]:
    suffix = uuid4().hex[:8]
    site = client.post(
        "/api/v1/sites", json={"code": f"sc_{suffix}", "name": "场景站点"}, headers=auth
    )
    assert site.status_code == 201, site.text
    site_id = site.json()["id"]
    m = client.post(
        "/api/v1/maps",
        json={"site_id": site_id, "code": f"m_{suffix}", "name": "场景图"},
        headers=auth,
    )
    assert m.status_code == 201, m.text
    CREATED.append((site_id, m.json()["id"]))
    return site_id, m.json()["id"]


def _version(client: TestClient, auth: dict, map_id: str, version: str) -> str:
    r = client.post(
        "/api/v1/map-versions",
        json={"map_id": map_id, "version": version, "semantic_revision": 1},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _publish(client: TestClient, auth: dict, version_id: str, with_slot: bool = True) -> None:
    if with_slot:
        _slot(client, auth, version_id)
    r = client.post(f"/api/v1/map-versions/{version_id}/publish", headers=auth)
    assert r.status_code in (200, 204), r.text


def _slot(client: TestClient, auth: dict, version_id: str, code: str = "S-01") -> str:
    r = client.post(
        "/api/v1/parking-slots",
        json={
            "map_version_id": version_id,
            "code": code,
            "polygon_json": {"points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]},
            "center_pose_json": {"x": 0.5, "y": 0.5, "theta": 0},
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _point(client: TestClient, auth: dict, version_id: str, slot_id: str) -> dict:
    r = client.post(
        "/api/v1/inspection-points",
        json={
            "map_version_id": version_id,
            "parking_slot_id": slot_id,
            "pose_json": {"x": 0.5, "y": 0.5, "theta": 0},
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_slot_cannot_move_to_another_draft_version(client: TestClient, auth: dict) -> None:
    _, map_id = _site_map(client, auth)
    v1 = _version(client, auth, map_id, "d1")
    v2 = _version(client, auth, map_id, "d2")
    slot = _slot(client, auth, v1)
    moved = client.put(
        f"/api/v1/parking-slots/{slot}",
        json={
            "map_version_id": v2,
            "code": "S-01",
            "polygon_json": {"points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]},
            "center_pose_json": {"x": 0.5, "y": 0.5, "theta": 0},
        },
        headers=auth,
    )
    assert moved.status_code == 409, moved.text
    assert moved.json()["error"]["code"] == "CROSS_VERSION_MOVE_FORBIDDEN"
    still = client.get(f"/api/v1/parking-slots?map_version_id={v1}", headers=auth).json()
    assert [s["id"] for s in still] == [slot]


def test_slot_cannot_migrate_into_published_version(client: TestClient, auth: dict) -> None:
    _, map_id = _site_map(client, auth)
    published = _version(client, auth, map_id, "pub")
    _publish(client, auth, published)
    draft = _version(client, auth, map_id, "drf")
    slot = _slot(client, auth, draft)
    moved = client.put(
        f"/api/v1/parking-slots/{slot}",
        json={
            "map_version_id": published,
            "code": "S-99",
            "polygon_json": {"points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]},
            "center_pose_json": {"x": 0.5, "y": 0.5, "theta": 0},
        },
        headers=auth,
    )
    assert moved.status_code == 409, moved.text
    assert moved.json()["error"]["code"] == "CROSS_VERSION_MOVE_FORBIDDEN"
    assert (
        client.get(f"/api/v1/parking-slots?map_version_id={published}", headers=auth).json()[0][
            "code"
        ]
        == "S-01"
    )


def test_published_slot_content_still_frozen(client: TestClient, auth: dict) -> None:
    _, map_id = _site_map(client, auth)
    published = _version(client, auth, map_id, "pub")
    _publish(client, auth, published)
    slot = client.get(f"/api/v1/parking-slots?map_version_id={published}", headers=auth).json()[0][
        "id"
    ]
    # same version, only geometry changes -> still refused because the version is not DRAFT
    r = client.put(
        f"/api/v1/parking-slots/{slot}",
        json={
            "map_version_id": published,
            "code": "S-01",
            "polygon_json": {"points": [{"x": 9, "y": 9}, {"x": 9, "y": 8}, {"x": 8, "y": 9}]},
            "center_pose_json": {"x": 9, "y": 9, "theta": 0},
        },
        headers=auth,
    )
    assert r.status_code == 409, r.text


def test_inspection_point_slot_must_belong_to_same_version(client: TestClient, auth: dict) -> None:
    _, map_id = _site_map(client, auth)
    v1 = _version(client, auth, map_id, "d1")
    v2 = _version(client, auth, map_id, "d2")
    slot_v2 = _slot(client, auth, v2)
    refused = client.post(
        "/api/v1/inspection-points",
        json={
            "map_version_id": v1,
            "parking_slot_id": slot_v2,
            "pose_json": {"x": 0, "y": 0, "theta": 0},
        },
        headers=auth,
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "SLOT_VERSION_MISMATCH"
    slot_v1 = _slot(client, auth, v1, code="S-01")
    ok = _point(client, auth, v1, slot_v1)
    assert ok["map_version_id"] == v1


def test_inspection_point_cannot_move_across_versions(client: TestClient, auth: dict) -> None:
    _, map_id = _site_map(client, auth)
    v1 = _version(client, auth, map_id, "d1")
    v2 = _version(client, auth, map_id, "d2")
    slot_v1 = _slot(client, auth, v1)
    slot_v2 = _slot(client, auth, v2)
    point = _point(client, auth, v1, slot_v1)
    moved = client.put(
        f"/api/v1/inspection-points/{point['id']}",
        json={
            "map_version_id": v2,
            "parking_slot_id": slot_v2,
            "pose_json": {"x": 0, "y": 0, "theta": 0},
        },
        headers=auth,
    )
    assert moved.status_code == 409, moved.text
    assert moved.json()["error"]["code"] == "CROSS_VERSION_MOVE_FORBIDDEN"


def test_delete_slot_referenced_by_point_refused(client: TestClient, auth: dict) -> None:
    _, map_id = _site_map(client, auth)
    v1 = _version(client, auth, map_id, "d1")
    slot = _slot(client, auth, v1)
    _point(client, auth, v1, slot)
    r = client.delete(f"/api/v1/parking-slots/{slot}", headers=auth)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "SLOT_IN_USE"
    assert client.delete(f"/api/v1/parking-slots/{slot}", headers=auth).status_code == 409


def test_extinguish_point_and_trajectory_cannot_move_across_versions(
    client: TestClient, auth: dict
) -> None:
    _, map_id = _site_map(client, auth)
    v1 = _version(client, auth, map_id, "d1")
    v2 = _version(client, auth, map_id, "d2")
    slot_v1 = _slot(client, auth, v1)
    slot_v2 = _slot(client, auth, v2)
    ext = client.post(
        "/api/v1/extinguish-points",
        json={
            "map_version_id": v1,
            "parking_slot_id": slot_v1,
            "pose_json": {"x": 0, "y": 0, "theta": 0},
        },
        headers=auth,
    )
    assert ext.status_code == 201, ext.text
    moved = client.put(
        f"/api/v1/extinguish-points/{ext.json()['id']}",
        json={
            "map_version_id": v2,
            "parking_slot_id": slot_v2,
            "pose_json": {"x": 0, "y": 0, "theta": 0},
        },
        headers=auth,
    )
    assert (
        moved.status_code == 409 and moved.json()["error"]["code"] == "CROSS_VERSION_MOVE_FORBIDDEN"
    )
    traj = client.post(
        "/api/v1/trajectories",
        json={
            "map_version_id": v1,
            "code": "T1",
            "path_json": [{"x": 0, "y": 0}, {"x": 1, "y": 0}],
        },
        headers=auth,
    )
    assert traj.status_code == 201, traj.text
    moved = client.put(
        f"/api/v1/trajectories/{traj.json()['id']}",
        json={
            "map_version_id": v2,
            "code": "T1",
            "path_json": [{"x": 0, "y": 0}, {"x": 1, "y": 0}],
        },
        headers=auth,
    )
    assert (
        moved.status_code == 409 and moved.json()["error"]["code"] == "CROSS_VERSION_MOVE_FORBIDDEN"
    )
    # same-version edit still works on a DRAFT (no regression of normal editing)
    ok = client.put(
        f"/api/v1/trajectories/{traj.json()['id']}",
        json={
            "map_version_id": v1,
            "code": "T1",
            "path_json": [{"x": 0, "y": 0}, {"x": 2, "y": 0}],
        },
        headers=auth,
    )
    assert ok.status_code == 200, ok.text
