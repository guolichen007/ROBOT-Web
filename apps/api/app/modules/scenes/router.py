"""WEB-R1B V2.1 scene persistence (storage_version=2). Batch 1: import / read / save / delete.

Single write authority for storage_version=2 versions: this router. The legacy /parking-slots,
/inspection-points, /trajectories writers refuse them (app/modules/maps/router.py:draft()).
Concurrency: every write is one transaction that first runs
  UPDATE map_versions SET revision=revision+1 WHERE id=? AND revision=? AND status='DRAFT'
so a stale editor gets 409 REVISION_CONFLICT and nothing is written (no silent overwrite).
Cross-version references are additionally impossible at the DB level (composite FKs, 0011).
Publish / scene asset digest / vehicle load are NOT in this batch and are not motion authority.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.dependencies import AuthContext, DbSession, request_meta, require_permission
from app.core.errors import PlatformError
from app.db.models import (
    Asset,
    Map,
    MapVersion,
    ScenePoint,
    SceneRoute,
    SceneRoutePoint,
    Site,
)
from app.modules.scenes.navmap import NavMapError, bind_image, parse_map_yaml, parse_raster

router = APIRouter(prefix="/api/v1/scenes", tags=["scenes"])
CODE = r"^[A-Za-z0-9_.-]{1,64}$"
UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class PointIn(BaseModel):
    point_id: str = Field(pattern=UUID)
    code: str = Field(pattern=CODE)
    kind: Literal["INSPECTION", "FIRE_OPERATION", "RETURN_HOME", "WAITING", "TRANSIT"]
    x: float = Field(ge=-100_000, le=100_000, allow_inf_nan=False)
    y: float = Field(ge=-100_000, le=100_000, allow_inf_nan=False)
    yaw: float = Field(ge=-3.1415927, le=3.1415927, allow_inf_nan=False)
    xy_tolerance_m: float | None = Field(default=None, gt=0, le=2, allow_inf_nan=False)
    yaw_tolerance_rad: float | None = Field(default=None, gt=0, le=3.1415927, allow_inf_nan=False)
    taught_from: Literal["MANUAL_INPUT", "MAP_CLICK", "VEHICLE_POSE"] = "MANUAL_INPUT"
    parking_slot_code: str | None = None

    @field_validator("parking_slot_code")
    @classmethod
    def no_slots_yet(cls, value: str | None) -> str | None:
        if value:
            raise ValueError("V2.1 parking slots are not persisted in batch 1")
        return value


class RoutePointIn(BaseModel):
    point_id: str = Field(pattern=UUID)
    arrival: Literal["PASS_THROUGH", "STOP"] = "PASS_THROUGH"


class RouteIn(BaseModel):
    route_id: str = Field(pattern=UUID)
    code: str = Field(pattern=CODE)
    closed: bool = False
    points: list[RoutePointIn] = Field(min_length=2, max_length=2000)


class SaveIn(BaseModel):
    expected_revision: int = Field(ge=1)
    points: list[PointIn] = Field(max_length=2000)
    routes: list[RouteIn] = Field(max_length=200)


class RevisionIn(BaseModel):
    expected_revision: int = Field(ge=1)


def _v2(db, version_id: str) -> MapVersion:
    version = db.get(MapVersion, version_id)
    if version is None:
        raise PlatformError("SCENE_NOT_FOUND", "场景版本不存在", status_code=404)
    if version.storage_version != 2:
        raise PlatformError(
            "LEGACY_STORAGE_VERSION", "该地图版本为旧存储格式（storage_version=1）", status_code=409
        )
    return version


def _bump(db, version_id: str, expected: int) -> int:
    """Atomic optimistic lock + DRAFT guard. Must be the first write of the transaction."""
    result = db.execute(
        update(MapVersion)
        .where(
            MapVersion.id == version_id,
            MapVersion.revision == expected,
            MapVersion.status == "DRAFT",
            MapVersion.storage_version == 2,
        )
        .values(revision=MapVersion.revision + 1)
        .returning(MapVersion.revision)
    ).first()
    if result is None:
        db.rollback()
        current = db.get(MapVersion, version_id)
        if current is None:
            raise PlatformError("SCENE_NOT_FOUND", "场景版本不存在", status_code=404)
        if current.status != "DRAFT":
            raise PlatformError("SCENE_NOT_DRAFT", "已发布/归档场景不可修改", status_code=409)
        raise PlatformError(
            "REVISION_CONFLICT",
            "场景已被他人修改，请重新加载后再保存",
            status_code=409,
            details={"expected_revision": expected, "current_revision": current.revision},
        )
    return int(result[0])


def scene_payload(db, version: MapVersion) -> dict[str, Any]:
    points = db.scalars(
        select(ScenePoint).where(ScenePoint.map_version_id == version.id).order_by(ScenePoint.code)
    ).all()
    routes = db.scalars(
        select(SceneRoute).where(SceneRoute.map_version_id == version.id).order_by(SceneRoute.code)
    ).all()
    refs = db.scalars(
        select(SceneRoutePoint)
        .where(SceneRoutePoint.map_version_id == version.id)
        .order_by(SceneRoutePoint.route_uid, SceneRoutePoint.seq)
    ).all()
    by_route: dict[str, list[dict[str, str]]] = {}
    for ref in refs:
        by_route.setdefault(ref.route_uid, []).append(
            {"point_id": ref.point_uid, "arrival": ref.arrival}
        )
    map_row = db.get(Map, version.map_id)
    return {
        "scene_id": version.id,
        "map_id": version.map_id,
        "map_code": map_row.code if map_row else None,
        "version": version.version,
        "status": version.status,
        "storage_version": version.storage_version,
        "revision": version.revision,
        "traversability": version.traversability,
        "nav_map": {
            "resolution_m_per_px": version.resolution_m_per_pixel,
            "origin": {"x": version.origin_x, "y": version.origin_y, "yaw": version.origin_yaw},
            "width_px": version.width_px,
            "height_px": version.height_px,
            "occupied_thresh": version.occupied_thresh,
            "free_thresh": version.free_thresh,
            "negate": version.negate,
            "image_sha256": version.nav_image_sha256,
            "yaml_sha256": version.nav_yaml_sha256,
            "map_checksum": version.nav_map_checksum,
            "image_url": f"/api/v1/scenes/{version.id}/nav-image",
        },
        "points": [
            {
                "point_id": p.point_uid,
                "code": p.code,
                "kind": p.kind,
                "x": p.x,
                "y": p.y,
                "yaw": p.yaw,
                "xy_tolerance_m": p.xy_tolerance_m,
                "yaw_tolerance_rad": p.yaw_tolerance_rad,
                "taught_from": p.taught_from,
            }
            for p in points
        ],
        "routes": [
            {
                "route_id": r.route_uid,
                "code": r.code,
                "closed": r.closed,
                "points": by_route.get(r.route_uid, []),
            }
            for r in routes
        ],
    }


def _store_asset(db, auth: AuthContext, name: str, content: bytes, mime: str) -> Asset:
    settings = get_settings()
    root = settings.asset_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    suffix = Path(name).suffix.lower()
    target = (root / f"{uuid4().hex}{suffix}").resolve()
    if root not in target.parents:
        raise PlatformError("ASSET_PATH_INVALID", "非法文件路径", status_code=400)
    target.write_bytes(content)
    row = Asset(
        object_name=target.name,
        original_filename=Path(name).name,
        mime_type=mime,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        created_by=auth.user.id,
    )
    db.add(row)
    db.flush()
    return row


@router.post("/import", status_code=201)
async def import_nav_map(
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
    site_id: str = Form(...),
    map_code: str = Form(..., pattern=CODE),
    version: str = Form(..., pattern=r"^[A-Za-z0-9_.-]{1,32}$"),
    yaml_file: UploadFile = File(...),
    image_file: UploadFile = File(...),
) -> dict:
    """YAML + PGM/PNG → new DRAFT scene (storage_version=2). Server-side validation is final."""
    settings = get_settings()
    if db.get(Site, site_id) is None:
        raise PlatformError("SITE_NOT_FOUND", "站点不存在", status_code=404)
    yaml_bytes = await yaml_file.read(64 * 1024 + 1)
    image_bytes = await image_file.read(settings.upload_max_bytes + 1)
    try:
        meta = parse_map_yaml(yaml_bytes)
        bind_image(meta, image_file.filename or "")
        raster = parse_raster(image_file.filename or "", image_bytes, settings.upload_max_bytes)
    except NavMapError as exc:
        raise PlatformError(exc.code, f"导航地图被拒绝：{exc}", status_code=422) from exc
    map_row = db.scalar(select(Map).where(Map.site_id == site_id, Map.code == map_code))
    if map_row is None:
        map_row = Map(site_id=site_id, code=map_code, name=map_code)
        db.add(map_row)
        db.flush()
    image_asset = _store_asset(
        db, auth, image_file.filename or "map", image_bytes, f"image/x-{raster.fmt}"
    )
    yaml_asset = _store_asset(db, auth, yaml_file.filename or "map.yaml", yaml_bytes, "text/yaml")
    row = MapVersion(
        map_id=map_row.id,
        version=version,
        status="DRAFT",
        storage_version=2,
        revision=1,
        semantic_revision=1,
        width_m=raster.width * meta.resolution,
        height_m=raster.height * meta.resolution,
        origin_x=meta.origin_x,
        origin_y=meta.origin_y,
        origin_yaw=meta.origin_yaw,
        rotation_rad=0.0,
        resolution_m_per_pixel=meta.resolution,
        frame_id="map",
        width_px=raster.width,
        height_px=raster.height,
        occupied_thresh=meta.occupied_thresh,
        free_thresh=meta.free_thresh,
        negate=meta.negate,
        nav_image_sha256=image_asset.sha256,
        nav_yaml_sha256=yaml_asset.sha256,
        nav_image_asset_id=image_asset.id,
        nav_yaml_asset_id=yaml_asset.id,
        created_by=auth.user.id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise PlatformError("SCENE_VERSION_EXISTS", "该地图版本号已存在", status_code=409) from exc
    write_audit(
        db,
        action="SCENE_IMPORT",
        resource_type="MAP_VERSION",
        user_id=auth.user.id,
        resource_id=row.id,
        after={"image_sha256": image_asset.sha256, "yaml_sha256": yaml_asset.sha256},
        **request_meta(request),
    )
    db.commit()
    return scene_payload(db, row)


@router.get("/{scene_id}")
def read_scene(
    scene_id: str, db: DbSession, _: AuthContext = Depends(require_permission("map.read"))
) -> dict:
    return scene_payload(db, _v2(db, scene_id))


@router.get("/{scene_id}/nav-image")
def nav_image(
    scene_id: str, db: DbSession, _: AuthContext = Depends(require_permission("map.read"))
):
    from fastapi.responses import FileResponse

    version = _v2(db, scene_id)
    asset = db.get(Asset, version.nav_image_asset_id) if version.nav_image_asset_id else None
    if asset is None:
        raise PlatformError("NAV_IMAGE_MISSING", "导航地图图像缺失", status_code=404)
    root = get_settings().asset_root.resolve()
    path = (root / asset.object_name).resolve()
    if root not in path.parents or not path.exists():
        raise PlatformError("NAV_IMAGE_MISSING", "导航地图图像缺失", status_code=404)
    return FileResponse(
        path, media_type="application/octet-stream", filename=asset.original_filename
    )


def _validate(payload: SaveIn) -> None:
    ids = [p.point_id for p in payload.points]
    codes = [p.code for p in payload.points]
    if len(set(ids)) != len(ids) or len(set(codes)) != len(codes):
        raise PlatformError("DUPLICATE_POINT", "点位 ID 或编号重复", status_code=422)
    rids = [r.route_id for r in payload.routes]
    rcodes = [r.code for r in payload.routes]
    if len(set(rids)) != len(rids) or len(set(rcodes)) != len(rcodes):
        raise PlatformError("DUPLICATE_ROUTE", "路线 ID 或编号重复", status_code=422)
    known = set(ids)
    for route in payload.routes:
        refs = [ref.point_id for ref in route.points]
        missing = [ref for ref in refs if ref not in known]
        if missing:
            raise PlatformError(
                "ROUTE_POINT_REF_MISSING",
                f"路线 {route.code} 引用了不存在的点位",
                status_code=422,
                details={"route": route.code, "missing": missing},
            )
        if any(a == b for a, b in zip(refs, refs[1:], strict=False)):
            raise PlatformError("ROUTE_REPEATED_ADJACENT_POINT", route.code, status_code=422)


@router.put("/{scene_id}")
def save_scene(
    scene_id: str,
    payload: SaveIn,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    """Full draft save in ONE transaction, guarded by expected_revision (no silent overwrite)."""
    _v2(db, scene_id)
    _validate(payload)
    new_revision = _bump(db, scene_id, payload.expected_revision)
    db.execute(delete(SceneRoutePoint).where(SceneRoutePoint.map_version_id == scene_id))
    db.execute(delete(SceneRoute).where(SceneRoute.map_version_id == scene_id))
    db.execute(delete(ScenePoint).where(ScenePoint.map_version_id == scene_id))
    for p in payload.points:
        db.add(
            ScenePoint(
                map_version_id=scene_id,
                point_uid=p.point_id,
                code=p.code,
                kind=p.kind,
                x=p.x,
                y=p.y,
                yaw=p.yaw,
                xy_tolerance_m=p.xy_tolerance_m,
                yaw_tolerance_rad=p.yaw_tolerance_rad,
                taught_from=p.taught_from,
            )
        )
    db.flush()
    for r in payload.routes:
        db.add(
            SceneRoute(map_version_id=scene_id, route_uid=r.route_id, code=r.code, closed=r.closed)
        )
    db.flush()
    for r in payload.routes:
        for seq, ref in enumerate(r.points):
            db.add(
                SceneRoutePoint(
                    map_version_id=scene_id,
                    route_uid=r.route_id,
                    seq=seq,
                    point_uid=ref.point_id,
                    arrival=ref.arrival,
                )
            )
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise PlatformError("SCENE_INTEGRITY", "场景引用不一致，未保存", status_code=422) from exc
    write_audit(
        db,
        action="SCENE_SAVE",
        resource_type="MAP_VERSION",
        user_id=auth.user.id,
        resource_id=scene_id,
        after={
            "revision": new_revision,
            "points": len(payload.points),
            "routes": len(payload.routes),
        },
        **request_meta(request),
    )
    try:
        db.commit()  # deferred composite FKs are verified here
    except IntegrityError as exc:
        db.rollback()
        raise PlatformError("SCENE_INTEGRITY", "场景引用不一致，未保存", status_code=422) from exc
    return scene_payload(db, _v2(db, scene_id))


@router.delete("/{scene_id}/points/{point_id}")
def delete_point(
    scene_id: str,
    point_id: str,
    payload: RevisionIn,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    _v2(db, scene_id)
    used_by = db.scalar(
        select(SceneRoutePoint.route_uid).where(
            SceneRoutePoint.map_version_id == scene_id, SceneRoutePoint.point_uid == point_id
        )
    )
    if used_by:
        raise PlatformError(
            "POINT_REFERENCED_BY_ROUTE", "点位仍被路线引用，请先从路线移除", status_code=409
        )
    new_revision = _bump(db, scene_id, payload.expected_revision)
    deleted = db.execute(
        delete(ScenePoint).where(
            ScenePoint.map_version_id == scene_id, ScenePoint.point_uid == point_id
        )
    ).rowcount
    if not deleted:
        db.rollback()
        raise PlatformError("POINT_NOT_FOUND", "点位不存在", status_code=404)
    write_audit(
        db,
        action="SCENE_POINT_DELETE",
        resource_type="MAP_VERSION",
        user_id=auth.user.id,
        resource_id=scene_id,
        after={"revision": new_revision, "point_id": point_id},
        **request_meta(request),
    )
    db.commit()
    return scene_payload(db, _v2(db, scene_id))


@router.delete("/{scene_id}/routes/{route_id}")
def delete_route(
    scene_id: str,
    route_id: str,
    payload: RevisionIn,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    _v2(db, scene_id)
    new_revision = _bump(db, scene_id, payload.expected_revision)
    deleted = db.execute(
        delete(SceneRoute).where(
            SceneRoute.map_version_id == scene_id, SceneRoute.route_uid == route_id
        )
    ).rowcount
    if not deleted:
        db.rollback()
        raise PlatformError("ROUTE_NOT_FOUND", "路线不存在", status_code=404)
    write_audit(
        db,
        action="SCENE_ROUTE_DELETE",
        resource_type="MAP_VERSION",
        user_id=auth.user.id,
        resource_id=scene_id,
        after={"revision": new_revision, "route_id": route_id},
        **request_meta(request),
    )
    db.commit()
    return scene_payload(db, _v2(db, scene_id))
