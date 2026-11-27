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
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.dependencies import AuthContext, DbSession, request_meta, require_permission
from app.core.errors import PlatformError
from app.db.models import (
    Asset,
    Map,
    MapVersion,
    SceneCorridor,
    SceneLoadRequest,
    SceneParkingSlot,
    ScenePoint,
    SceneRoute,
    SceneRouteCorridor,
    SceneRoutePoint,
    Site,
    Task,
)
from app.modules.scenes.navmap import NavMapError, bind_image, parse_map_yaml, parse_raster

router = APIRouter(prefix="/api/v1/scenes", tags=["scenes"])
TEACH_POSE_MAX_AGE_S = 2.0
LOAD_REQUEST_TTL_S = 60
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
    # VEHICLE_POSE can only be produced by POST /{id}/teach (server reads the live pose itself)
    taught_from: Literal["MANUAL_INPUT", "MAP_CLICK", "VEHICLE_POSE"] = "MANUAL_INPUT"
    # WEB-R1C: same-version slot reference by slot_id (composite FK, 0012)
    slot_id: str | None = Field(default=None, pattern=UUID)
    parking_slot_code: str | None = None

    @field_validator("parking_slot_code")
    @classmethod
    def slot_by_id_only(cls, value: str | None) -> str | None:
        if value:
            raise ValueError("reference parking slots by slot_id (same scene version)")
        return value


class XYIn(BaseModel):
    x: float = Field(ge=-100_000, le=100_000, allow_inf_nan=False)
    y: float = Field(ge=-100_000, le=100_000, allow_inf_nan=False)


class SlotIn(BaseModel):
    slot_id: str = Field(pattern=UUID)
    code: str = Field(pattern=CODE)
    polygon: list[XYIn] = Field(min_length=3, max_length=64)
    center_x: float = Field(ge=-100_000, le=100_000, allow_inf_nan=False)
    center_y: float = Field(ge=-100_000, le=100_000, allow_inf_nan=False)
    center_yaw: float = Field(ge=-3.1415927, le=3.1415927, allow_inf_nan=False)
    enabled: bool = True


class CorridorIn(BaseModel):
    corridor_id: str = Field(pattern=UUID)
    code: str = Field(pattern=CODE)
    kind: Literal["RIGHT_LANE"] = "RIGHT_LANE"
    centerline: list[XYIn] = Field(min_length=2, max_length=2000)
    width_m: float = Field(ge=0.5, le=10, allow_inf_nan=False)


class RoutePointIn(BaseModel):
    point_id: str = Field(pattern=UUID)
    # WEB-R1C: no default — every waypoint states its arrival explicitly, so a client can never
    # silently degrade an intermediate STOP to PASS_THROUGH by omitting the field
    arrival: Literal["PASS_THROUGH", "STOP"]


class RouteIn(BaseModel):
    route_id: str = Field(pattern=UUID)
    code: str = Field(pattern=CODE)
    closed: bool = False
    points: list[RoutePointIn] = Field(min_length=2, max_length=2000)
    corridor_ids: list[str] = Field(default_factory=list, max_length=200)


class SaveIn(BaseModel):
    expected_revision: int = Field(ge=1)
    points: list[PointIn] = Field(max_length=2000)
    routes: list[RouteIn] = Field(max_length=200)
    parking_slots: list[SlotIn] = Field(default_factory=list, max_length=2000)
    corridors: list[CorridorIn] = Field(default_factory=list, max_length=200)


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
    slots = db.scalars(
        select(SceneParkingSlot)
        .where(SceneParkingSlot.map_version_id == version.id)
        .order_by(SceneParkingSlot.code)
    ).all()
    corridors = db.scalars(
        select(SceneCorridor)
        .where(SceneCorridor.map_version_id == version.id)
        .order_by(SceneCorridor.code)
    ).all()
    route_corridors: dict[str, list[str]] = {}
    for rc in db.scalars(
        select(SceneRouteCorridor)
        .where(SceneRouteCorridor.map_version_id == version.id)
        .order_by(SceneRouteCorridor.route_uid, SceneRouteCorridor.seq)
    ).all():
        route_corridors.setdefault(rc.route_uid, []).append(rc.corridor_uid)
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
        "scene_digest": version.scene_digest,
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "manifest_url": (
            f"/api/v1/scenes/{version.id}/manifest" if version.scene_asset_id else None
        ),
        "parking_slots": [
            {
                "slot_id": s.slot_uid,
                "code": s.code,
                "polygon": s.polygon_json,
                "center_x": s.center_x,
                "center_y": s.center_y,
                "center_yaw": s.center_yaw,
                "enabled": s.enabled,
            }
            for s in slots
        ],
        "corridors": [
            {
                "corridor_id": c.corridor_uid,
                "code": c.code,
                "kind": c.kind,
                "centerline": c.centerline_json,
                "width_m": c.width_m,
            }
            for c in corridors
        ],
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
                "slot_id": p.slot_uid,
            }
            for p in points
        ],
        "routes": [
            {
                "route_id": r.route_uid,
                "code": r.code,
                "closed": r.closed,
                "points": by_route.get(r.route_uid, []),
                "corridor_ids": route_corridors.get(r.route_uid, []),
            }
            for r in routes
        ],
    }


def _store_asset(
    db, auth: AuthContext, name: str, content: bytes, mime: str, written: list[Path]
) -> Asset:
    """Write an asset file atomically (tmp + fsync + rename) and record it in `written`, so the
    caller can remove every file of a transaction that does not commit (no orphan files)."""
    import os

    settings = get_settings()
    root = settings.asset_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    suffix = Path(name).suffix.lower()
    target = (root / f"{uuid4().hex}{suffix}").resolve()
    if root not in target.parents:
        raise PlatformError("ASSET_PATH_INVALID", "非法文件路径", status_code=400)
    tmp = target.with_name(target.name + ".part")
    try:
        with open(tmp, "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise PlatformError(
            "ASSET_WRITE_FAILED", "地图文件写入失败，未导入", status_code=500
        ) from exc
    written.append(target)
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
    written: list[Path] = []
    try:
        return _import_tx(
            request,
            db,
            auth,
            site_id,
            map_code,
            version,
            meta,
            raster,
            image_file.filename or "map",
            image_bytes,
            yaml_file.filename or "map.yaml",
            yaml_bytes,
            written,
        )
    except BaseException:
        # WEB-R1C: any failure before commit (duplicate version, DB error, write error) removes
        # every file this import wrote — the DB rolled back, so they would be orphans
        db.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        raise


def _import_tx(
    request,
    db,
    auth,
    site_id,
    map_code,
    version,
    meta,
    raster,
    image_name,
    image_bytes,
    yaml_name,
    yaml_bytes,
    written: list[Path],
) -> dict:
    # duplicate (site, map, version) is refused BEFORE anything is written
    map_row = db.scalar(select(Map).where(Map.site_id == site_id, Map.code == map_code))
    if map_row is not None and db.scalar(
        select(MapVersion.id).where(MapVersion.map_id == map_row.id, MapVersion.version == version)
    ):
        raise PlatformError("SCENE_VERSION_EXISTS", "该地图版本号已存在", status_code=409)
    if map_row is None:
        # concurrent first imports of the same map race here: the loser re-reads the winner's
        # row inside a savepoint instead of failing the whole request (found by R1C race test)
        try:
            with db.begin_nested():
                map_row = Map(site_id=site_id, code=map_code, name=map_code)
                db.add(map_row)
                db.flush()
        except IntegrityError:
            map_row = db.scalar(select(Map).where(Map.site_id == site_id, Map.code == map_code))
            if map_row is None:
                raise
    image_asset = _store_asset(db, auth, image_name, image_bytes, f"image/x-{raster.fmt}", written)
    yaml_asset = _store_asset(db, auth, yaml_name, yaml_bytes, "text/yaml", written)
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
    except IntegrityError as exc:  # concurrent import of the same version
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
    sids = [s.slot_id for s in payload.parking_slots]
    scodes = [s.code for s in payload.parking_slots]
    if len(set(sids)) != len(sids) or len(set(scodes)) != len(scodes):
        raise PlatformError("DUPLICATE_SLOT", "车位 ID 或编号重复", status_code=422)
    cids = [c.corridor_id for c in payload.corridors]
    ccodes = [c.code for c in payload.corridors]
    if len(set(cids)) != len(cids) or len(set(ccodes)) != len(ccodes):
        raise PlatformError("DUPLICATE_CORRIDOR", "走廊 ID 或编号重复", status_code=422)
    for corridor in payload.corridors:
        line = corridor.centerline
        if any(
            math.hypot(b.x - a.x, b.y - a.y) < 1e-3 for a, b in zip(line, line[1:], strict=False)
        ):
            raise PlatformError("CORRIDOR_ZERO_LENGTH_SEGMENT", corridor.code, status_code=422)
    known_slots = set(sids)
    for p in payload.points:
        if p.slot_id and p.slot_id not in known_slots:
            raise PlatformError("POINT_SLOT_REF_MISSING", p.code, status_code=422)
    known = set(ids)
    known_corridors = set(cids)
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
        if route.points[-1].arrival != "STOP":
            # contract: a route must end in a must-reach STOP (success anchor)
            raise PlatformError("ROUTE_FINAL_WAYPOINT_NOT_STOP", route.code, status_code=422)
        missing_c = [c for c in route.corridor_ids if c not in known_corridors]
        if missing_c:
            raise PlatformError("ROUTE_CORRIDOR_REF_MISSING", route.code, status_code=422)


def _write_children(db, scene_id: str, payload: SaveIn, taught: dict[str, str]) -> None:
    """Replace every child row of a DRAFT version (same transaction as the revision bump)."""
    for model in (
        SceneRouteCorridor,
        SceneRoutePoint,
        SceneRoute,
        ScenePoint,
        SceneCorridor,
        SceneParkingSlot,
    ):
        db.execute(delete(model).where(model.map_version_id == scene_id))
    for s in payload.parking_slots:
        db.add(
            SceneParkingSlot(
                map_version_id=scene_id,
                slot_uid=s.slot_id,
                code=s.code,
                polygon_json=[{"x": v.x, "y": v.y} for v in s.polygon],
                center_x=s.center_x,
                center_y=s.center_y,
                center_yaw=s.center_yaw,
                enabled=s.enabled,
            )
        )
    for c in payload.corridors:
        db.add(
            SceneCorridor(
                map_version_id=scene_id,
                corridor_uid=c.corridor_id,
                code=c.code,
                kind=c.kind,
                centerline_json=[{"x": v.x, "y": v.y} for v in c.centerline],
                width_m=c.width_m,
            )
        )
    db.flush()
    for p in payload.points:
        # provenance: VEHICLE_POSE is kept only for points the server itself taught
        source = p.taught_from
        if source == "VEHICLE_POSE" and taught.get(p.point_id) != "VEHICLE_POSE":
            source = "MANUAL_INPUT"
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
                taught_from=source,
                slot_uid=p.slot_id,
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
        for seq, corridor_id in enumerate(r.corridor_ids):
            db.add(
                SceneRouteCorridor(
                    map_version_id=scene_id, route_uid=r.route_id, seq=seq, corridor_uid=corridor_id
                )
            )


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
    taught: dict[str, str] = {
        uid: source
        for uid, source in db.execute(
            select(ScenePoint.point_uid, ScenePoint.taught_from).where(
                ScenePoint.map_version_id == scene_id
            )
        ).tuples()
    }
    new_revision = _bump(db, scene_id, payload.expected_revision)
    _write_children(db, scene_id, payload, taught)
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
            "parking_slots": len(payload.parking_slots),
            "corridors": len(payload.corridors),
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


# ---------------------------------------------------------------- WEB-R1C batch 2 (0012)


class ChecksumIn(BaseModel):
    expected_revision: int = Field(ge=1)
    # wire 1.3 location.map_checksum, as reported by the vehicle (algorithm is vehicle-defined)
    map_checksum: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


@router.put("/{scene_id}/nav-map-checksum")
def set_nav_map_checksum(
    scene_id: str,
    payload: ChecksumIn,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    """Register the navigation-map identity the vehicle reports (DRAFT only, revision-guarded).
    Publishing requires it: a scene is bound to exactly one nav-map identity."""
    _v2(db, scene_id)
    new_revision = _bump(db, scene_id, payload.expected_revision)
    db.execute(
        update(MapVersion)
        .where(MapVersion.id == scene_id)
        .values(nav_map_checksum=payload.map_checksum)
        .execution_options(synchronize_session=False)
    )
    write_audit(
        db,
        action="SCENE_NAV_MAP_CHECKSUM",
        resource_type="MAP_VERSION",
        user_id=auth.user.id,
        resource_id=scene_id,
        after={"revision": new_revision, "map_checksum": payload.map_checksum},
        **request_meta(request),
    )
    db.commit()
    db.expire_all()
    return scene_payload(db, _v2(db, scene_id))


@router.get("")
def list_scenes(
    db: DbSession,
    _: AuthContext = Depends(require_permission("map.read")),
    site_id: str | None = None,
) -> list[dict]:
    """Saved V2.1 scenes (storage_version=2) for the reopen entry of the editor."""
    query = (
        select(MapVersion, Map)
        .join(Map, Map.id == MapVersion.map_id)
        .where(MapVersion.storage_version == 2)
        .order_by(MapVersion.created_at.desc())
        .limit(200)
    )
    if site_id:
        query = query.where(Map.site_id == site_id)
    rows = db.execute(query).all()
    return [
        {
            "scene_id": v.id,
            "map_code": m.code,
            "site_id": m.site_id,
            "version": v.version,
            "status": v.status,
            "revision": v.revision,
            "scene_digest": v.scene_digest,
            "traversability": v.traversability,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "published_at": v.published_at.isoformat() if v.published_at else None,
            "points": db.scalar(
                select(func.count())
                .select_from(ScenePoint)
                .where(ScenePoint.map_version_id == v.id)
            ),
            "routes": db.scalar(
                select(func.count())
                .select_from(SceneRoute)
                .where(SceneRoute.map_version_id == v.id)
            ),
        }
        for v, m in rows
    ]


class TeachIn(BaseModel):
    expected_revision: int = Field(ge=1)
    vehicle_id: str = Field(min_length=1, max_length=64)
    point_id: str = Field(pattern=UUID)
    code: str = Field(pattern=CODE)
    kind: Literal["INSPECTION", "FIRE_OPERATION", "RETURN_HOME", "WAITING", "TRANSIT"]


@router.post("/{scene_id}/teach")
def teach_point(
    scene_id: str,
    payload: TeachIn,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    """当前位置示教: the SERVER reads the vehicle's live pose (never the browser's numbers).
    Only a fresh, localised pose on THIS scene's nav map is accepted; read-only on the vehicle."""
    from app.core.events import get_redis
    from app.modules.robots.router import find_robot

    version = _v2(db, scene_id)
    if version.status != "DRAFT":
        raise PlatformError("SCENE_NOT_DRAFT", "已发布/归档场景不可修改", status_code=409)
    robot = find_robot(db, payload.vehicle_id, auth)
    raw = get_redis().get(f"robot:{robot.vehicle_id}:latest")
    latest = json.loads(raw) if raw else {}
    if robot.online_state != "ONLINE":
        raise PlatformError("POSE_NOT_LIVE", f"车辆 {robot.online_state}", status_code=409)
    if latest.get("localization_status") != "OK" or latest.get("x") is None:
        raise PlatformError("POSE_NOT_LIVE", "定位不可用", status_code=409)
    received = latest.get("server_received_at")
    age = (
        (datetime.now(UTC) - datetime.fromisoformat(received)).total_seconds()
        if received
        else math.inf
    )
    if not 0 <= age <= TEACH_POSE_MAX_AGE_S:
        raise PlatformError("POSE_STALE", f"位姿 {age:.1f}s 前", status_code=409)
    map_row = db.get(Map, version.map_id)
    if (latest.get("map_code"), latest.get("map_version")) != (
        map_row.code if map_row else None,
        version.version,
    ):
        raise PlatformError(
            "POSE_MAP_MISMATCH",
            f"车辆地图 {latest.get('map_code')}/{latest.get('map_version')} 与场景不一致",
            status_code=409,
        )
    if version.nav_map_checksum and latest.get("map_checksum") not in (
        None,
        version.nav_map_checksum,
    ):
        raise PlatformError("POSE_MAP_MISMATCH", "导航地图校验和不一致", status_code=409)
    x, y = float(latest["x"]), float(latest["y"])
    yaw = math.atan2(math.sin(float(latest["theta"])), math.cos(float(latest["theta"])))
    if not all(map(math.isfinite, (x, y, yaw))):
        raise PlatformError("POSE_NOT_LIVE", "位姿非有限数", status_code=409)
    if db.scalar(
        select(ScenePoint.id).where(
            ScenePoint.map_version_id == scene_id, ScenePoint.code == payload.code
        )
    ):
        raise PlatformError("DUPLICATE_POINT", payload.code, status_code=422)
    new_revision = _bump(db, scene_id, payload.expected_revision)
    db.add(
        ScenePoint(
            map_version_id=scene_id,
            point_uid=payload.point_id,
            code=payload.code,
            kind=payload.kind,
            x=round(x, 4),
            y=round(y, 4),
            yaw=round(yaw, 6),
            taught_from="VEHICLE_POSE",
        )
    )
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise PlatformError("DUPLICATE_POINT", payload.code, status_code=422) from exc
    write_audit(
        db,
        action="SCENE_TEACH_POINT",
        resource_type="MAP_VERSION",
        user_id=auth.user.id,
        resource_id=scene_id,
        after={
            "revision": new_revision,
            "point_id": payload.point_id,
            "vehicle_id": robot.vehicle_id,
            "boot_id": latest.get("boot_id"),
            "pose": {"x": x, "y": y, "yaw": yaw},
            "pose_received_at": received,
        },
        **request_meta(request),
    )
    db.commit()
    return scene_payload(db, _v2(db, scene_id))


def build_manifest(db, version: MapVersion, *, published_at: str, published_by: str) -> dict:
    """SceneManifest (contract 2.1.0-draft.3) of a version, exactly as stored."""
    from services import contracts_v21 as c

    body = scene_payload(db, version)
    map_row = db.get(Map, version.map_id)
    site = db.get(Site, map_row.site_id) if map_row else None
    slot_code = {s["slot_id"]: s["code"] for s in body["parking_slots"]}
    manifest: dict[str, Any] = {
        "contract_version": c.CONTRACT_VERSION,
        "kind": "SceneManifest",
        "scene_id": version.id,
        "scene_version": version.revision,
        "base_scene_version": None,
        "status": "PUBLISHED",
        "site_code": site.code if site else "UNKNOWN",
        "nav_map": {
            "map_code": body["map_code"],
            "map_version": version.version,
            "frame_id": "map",
            "resolution_m_per_px": version.resolution_m_per_pixel,
            "origin": {
                "x": version.origin_x,
                "y": version.origin_y,
                "yaw": version.origin_yaw or 0.0,
                "frame_id": "map",
            },
            "width_px": version.width_px,
            "height_px": version.height_px,
            "image_sha256": version.nav_image_sha256,
            "yaml_sha256": version.nav_yaml_sha256,
            "map_checksum": version.nav_map_checksum,
            "occupied_thresh": version.occupied_thresh,
            "free_thresh": version.free_thresh,
            "negate": version.negate,
        },
        "points": [
            {
                "point_id": p["point_id"],
                "code": p["code"],
                "kind": p["kind"],
                "pose": {"x": p["x"], "y": p["y"], "yaw": p["yaw"], "frame_id": "map"},
                "parking_slot_code": slot_code.get(p["slot_id"]) if p["slot_id"] else None,
                **({"xy_tolerance_m": p["xy_tolerance_m"]} if p["xy_tolerance_m"] else {}),
                **({"yaw_tolerance_rad": p["yaw_tolerance_rad"]} if p["yaw_tolerance_rad"] else {}),
                "taught_from": p["taught_from"],
            }
            for p in body["points"]
        ],
        "parking_slots": [
            {
                "slot_id": s["slot_id"],
                "code": s["code"],
                "polygon": s["polygon"],
                "center": {
                    "x": s["center_x"],
                    "y": s["center_y"],
                    "yaw": s["center_yaw"],
                    "frame_id": "map",
                },
                "enabled": s["enabled"],
            }
            for s in body["parking_slots"]
        ],
        "routes": [
            {
                "route_id": r["route_id"],
                "code": r["code"],
                "point_refs": [x["point_id"] for x in r["points"]],
                "closed": r["closed"],
                "corridor_refs": r["corridor_ids"],
            }
            for r in body["routes"]
        ],
        "corridors": [
            {
                "corridor_id": co["corridor_id"],
                "code": co["code"],
                "kind": co["kind"],
                "centerline": co["centerline"],
                "width_m": co["width_m"],
            }
            for co in body["corridors"]
        ],
        "validation": {
            "schema_ok": True,
            "references_ok": True,
            # never claimed without real planner evidence (contract rule)
            "traversability": version.traversability,
            "traversability_evidence": None,
            "checked_at": published_at,
            "issues": [],
        },
        "digest": None,
        "created_at": version.created_at.isoformat(),
        "published_at": published_at,
        "published_by": published_by,
    }
    manifest["digest"] = c.scene_digest(manifest)
    return manifest


def route_arrivals(db, version_id: str) -> dict[str, list[str]]:
    """Per-route arrival list (manifest routes have point_refs only; arrivals ride alongside)."""
    out: dict[str, list[str]] = {}
    for ref in db.scalars(
        select(SceneRoutePoint)
        .where(SceneRoutePoint.map_version_id == version_id)
        .order_by(SceneRoutePoint.route_uid, SceneRoutePoint.seq)
    ).all():
        out.setdefault(ref.route_uid, []).append(ref.arrival)
    return out


@router.post("/{scene_id}/publish")
def publish_scene(
    scene_id: str,
    payload: RevisionIn,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.publish")),
) -> dict:
    """Atomic publish (one transaction): validate -> DRAFT->PUBLISHED guarded by revision ->
    canonical digest -> immutable manifest asset (sha256). Publishing is NOT a load and NOT a
    motion authorisation; traversability stays as proven (PENDING_VERIFICATION by default)."""
    from services import contracts_v21 as c

    version = _v2(db, scene_id)
    if version.status != "DRAFT":
        raise PlatformError("SCENE_NOT_DRAFT", "已发布/归档场景不可再次发布", status_code=409)
    if not version.nav_map_checksum:
        raise PlatformError(
            "NAV_MAP_CHECKSUM_MISSING",
            "导航地图校验和（车端 map_checksum）未登记，不能发布",
            status_code=409,
        )
    now = datetime.now(UTC)
    published_at = now.isoformat()
    # the exact rows of THIS revision (the bump below fails if anyone saved meanwhile)
    manifest = build_manifest(db, version, published_at=published_at, published_by=auth.user.id)
    manifest["scene_version"] = payload.expected_revision
    manifest["digest"] = c.scene_digest(manifest)
    try:
        c.validate("SceneManifest", manifest)
    except c.ContractError as exc:
        raise PlatformError(
            "SCENE_INVALID", f"{exc.code}: {exc}", status_code=422, details={"code": exc.code}
        ) from exc
    for name, arrivals in route_arrivals(db, scene_id).items():
        if not arrivals or arrivals[-1] != "STOP":
            raise PlatformError("ROUTE_FINAL_WAYPOINT_NOT_STOP", name, status_code=422)
    content = json.dumps(
        {**manifest, "route_arrivals": route_arrivals(db, scene_id)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    written: list[Path] = []
    try:
        asset = _store_asset(
            db,
            auth,
            f"scene-{scene_id}-v{payload.expected_revision}.json",
            content,
            "application/json",
            written,
        )
        # ONE guarded statement flips DRAFT->PUBLISHED together with digest + asset + time:
        # ck_map_versions_v2_published_digest can never observe a half-published row, and a
        # concurrent save/publish (revision moved) matches 0 rows.
        result = db.execute(
            update(MapVersion)
            .where(
                MapVersion.id == scene_id,
                MapVersion.revision == payload.expected_revision,
                MapVersion.status == "DRAFT",
                MapVersion.storage_version == 2,
            )
            .values(
                status="PUBLISHED",
                scene_digest=manifest["digest"],
                scene_asset_id=asset.id,
                published_at=now,
                published_by=auth.user.id,
            )
            .returning(MapVersion.id)
            .execution_options(synchronize_session=False)
        ).first()
        if result is None:
            current_status, current_revision = db.execute(
                select(MapVersion.status, MapVersion.revision).where(MapVersion.id == scene_id)
            ).one()
            if current_status != "DRAFT":
                raise PlatformError(
                    "SCENE_NOT_DRAFT", "已发布/归档场景不可再次发布", status_code=409
                )
            raise PlatformError(
                "REVISION_CONFLICT",
                "场景已被他人修改，请重新加载后再发布",
                status_code=409,
                details={
                    "expected_revision": payload.expected_revision,
                    "current_revision": current_revision,
                },
            )
        write_audit(
            db,
            action="SCENE_PUBLISH",
            resource_type="MAP_VERSION",
            user_id=auth.user.id,
            resource_id=scene_id,
            after={
                "scene_version": payload.expected_revision,
                "scene_digest": manifest["digest"],
                "asset_sha256": asset.sha256,
                "traversability": version.traversability,
            },
            **request_meta(request),
        )
        db.commit()
    except BaseException:
        db.rollback()
        for path in written:
            path.unlink(missing_ok=True)
        raise
    db.expire_all()
    return scene_payload(db, _v2(db, scene_id))


def _verified_asset_bytes(db, version: MapVersion) -> bytes:
    asset = db.get(Asset, version.scene_asset_id) if version.scene_asset_id else None
    if asset is None:
        raise PlatformError("SCENE_ASSET_MISSING", "场景资产缺失", status_code=404)
    root = get_settings().asset_root.resolve()
    path = (root / asset.object_name).resolve()
    if root not in path.parents or not path.exists():
        raise PlatformError("SCENE_ASSET_MISSING", "场景资产缺失", status_code=404)
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != asset.sha256:
        # an immutable asset that no longer matches its recorded hash is never served
        raise PlatformError("SCENE_ASSET_TAMPERED", "场景资产与记录摘要不一致", status_code=409)
    return content


@router.get("/{scene_id}/manifest")
def scene_manifest(
    scene_id: str, db: DbSession, _: AuthContext = Depends(require_permission("map.read"))
) -> dict:
    from services import contracts_v21 as c

    version = _v2(db, scene_id)
    content = _verified_asset_bytes(db, version)
    doc = json.loads(content)
    arrivals = doc.pop("route_arrivals", {})
    if doc.get("digest") != version.scene_digest or c.scene_digest(doc) != version.scene_digest:
        raise PlatformError("SCENE_DIGEST_MISMATCH", "场景摘要不一致", status_code=409)
    return {
        "manifest": doc,
        "route_arrivals": arrivals,
        "asset_sha256": hashlib.sha256(content).hexdigest(),
    }


class LoadIn(BaseModel):
    vehicle_id: str = Field(min_length=1, max_length=64)


@router.post("/{scene_id}/load", status_code=202)
def request_scene_load(
    scene_id: str,
    payload: LoadIn,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.publish")),
) -> dict:
    """Record a SceneLoadRequest for an isolated SIM vehicle. REAL vehicles are refused (no scene
    downlink exists in this release); nothing is published to any MQTT topic here — the SIM
    executor fetches the request over the isolated channel and answers with a receipt."""
    from app.core.config import is_synthetic_namespace
    from app.modules.robots.router import find_robot
    from services import contracts_v21 as c

    settings = get_settings()
    version = _v2(db, scene_id)
    if version.status != "PUBLISHED" or not version.scene_digest:
        raise PlatformError("SCENE_NOT_PUBLISHED", "只有已发布场景可加载", status_code=409)
    robot = find_robot(db, payload.vehicle_id, auth)
    if (
        not is_synthetic_namespace(robot.vehicle_id)
        or settings.firebot_deployment_env != "SIM"
        or not settings.sim_downlink_enabled
    ):
        raise PlatformError(
            "SCENE_LOAD_SIM_ONLY",
            "场景加载本期只在隔离 SIM 环境演练；真实车辆不下发",
            status_code=409,
        )
    if not robot.boot_id or robot.online_state != "ONLINE":
        raise PlatformError("VEHICLE_NOT_READY", "车辆未在线或启动会话未知", status_code=409)
    _verified_asset_bytes(db, version)  # never request a load of a tampered asset
    now = datetime.now(UTC)
    load = SceneLoadRequest(
        load_id=str(uuid4()),
        robot_id=robot.id,
        vehicle_id=robot.vehicle_id,
        environment="SIM",
        map_version_id=version.id,
        scene_version=version.revision,
        scene_digest=version.scene_digest,
        nav_map_checksum=version.nav_map_checksum or "",
        target_boot_id=robot.boot_id,
        issued_at=now,
        expires_at=now + timedelta(seconds=LOAD_REQUEST_TTL_S),
        issued_by=auth.user.id,
        state="PENDING",
        payload_json={},
    )
    load.payload_json = {
        "contract_version": c.CONTRACT_VERSION,
        "kind": "SceneLoadRequest",
        "load_id": load.load_id,
        "vehicle_id": robot.vehicle_id,
        "environment": "SIM",
        "target_boot_id": robot.boot_id,
        "scene_id": version.id,
        "scene_version": version.revision,
        "scene_digest": version.scene_digest,
        "nav_map_checksum": version.nav_map_checksum,
        "issued_at": now.isoformat(),
        "expires_at": load.expires_at.isoformat(),
        "operator_id": auth.user.id,
    }
    c.validate("SceneLoadRequest", load.payload_json)
    # supersede older pending loads of this vehicle: only the newest load_id may confirm
    db.execute(
        update(SceneLoadRequest)
        .where(SceneLoadRequest.robot_id == robot.id, SceneLoadRequest.state == "PENDING")
        .values(state="SUPERSEDED")
    )
    db.add(load)
    db.flush()
    robot.latest_scene_load_id = load.load_id
    write_audit(
        db,
        action="SCENE_LOAD_REQUEST",
        resource_type="MAP_VERSION",
        user_id=auth.user.id,
        resource_id=scene_id,
        after={"load_id": load.load_id, "vehicle_id": robot.vehicle_id, "boot": robot.boot_id},
        **request_meta(request),
    )
    db.commit()
    return load.payload_json


class SimMissionIn(BaseModel):
    vehicle_id: str = Field(min_length=1, max_length=64)
    route_id: str = Field(pattern=UUID)


@router.post("/{scene_id}/sim-missions", status_code=201)
def create_sim_mission(
    scene_id: str,
    payload: SimMissionIn,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
) -> dict:
    """WEB-R1C R1C-4: admit a PATROL MissionRequest on the isolated SIM stack only.

    Admission = contracts_v21.mission_admissible over the platform's OWN snapshot + server
    OperationReadiness + the vehicle's CONFIRMED loaded scene. The request is frozen into the
    task (route snapshot incl. every waypoint's arrival); no platform->vehicle publish happens
    here — the SIM executor reads it from GET /sim-missions/{mission_id}."""
    from app.core.config import is_synthetic_namespace
    from app.modules.commands.operation_readiness import operation_readiness
    from app.modules.commands.readiness import robot_readiness
    from app.modules.commands.service import task_code
    from app.modules.robots.router import find_robot
    from services import contracts_v21 as c
    from services.sim_v21 import SIM_MISSION_TYPE, vehicle_snapshot

    settings = get_settings()
    version = _v2(db, scene_id)
    robot = find_robot(db, payload.vehicle_id, auth)
    if not is_synthetic_namespace(robot.vehicle_id) or settings.firebot_deployment_env != "SIM":
        raise PlatformError("SIM_ONLY", "本期任务反馈接线只在隔离 SIM 环境", status_code=409)
    manifest = _manifest_or_none(db, version)
    now = datetime.now(UTC)
    snapshot = vehicle_snapshot(db, robot, now)
    readiness = operation_readiness(
        robot,
        robot_readiness(db, robot),
        settings=settings,
        permissions=set(auth.permissions),
        now=now,
    )
    readiness["based_on_snapshot_at"] = snapshot["generated_at"]
    ok, code = c.mission_admissible(manifest, snapshot, readiness, "patrol_start", now=now)
    if not ok:
        raise PlatformError(code, f"任务未准入：{code}", status_code=409, details={"code": code})
    assert manifest is not None
    route = next((r for r in manifest["routes"] if r["route_id"] == payload.route_id), None)
    if route is None:
        raise PlatformError("ROUTE_NOT_FOUND", "路线不存在", status_code=404)
    arrivals = route_arrivals(db, scene_id)[route["route_id"]]
    points = {p["point_id"]: p for p in manifest["points"]}
    mission_id, command_id = str(uuid4()), str(uuid4())
    req = {
        "contract_version": c.CONTRACT_VERSION,
        "kind": "MissionRequest",
        "mission_id": mission_id,
        "idempotency_key": f"sim-{mission_id}",
        "vehicle_id": robot.vehicle_id,
        "environment": "SIM",
        "target_boot_id": robot.boot_id,
        "mission_kind": "PATROL",
        "scene_id": manifest["scene_id"],
        "scene_version": manifest["scene_version"],
        "scene_digest": manifest["digest"],
        "route_snapshot": {
            "route_id": route["route_id"],
            "start_index": 0,
            "waypoints": [
                {
                    "index": i,
                    "point_id": ref,
                    "pose": points[ref]["pose"],
                    "arrival": arrivals[i],
                }
                for i, ref in enumerate(route["point_refs"])
            ],
        },
        "nav_map_checksum": manifest["nav_map"]["map_checksum"],
        "resume_of": None,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=120)).isoformat(),
        "operator_id": auth.user.id,
        "command_id": command_id,
        "execution_gen": 1,
    }
    c.validate("MissionRequest", req)
    task = Task(
        task_code=task_code("S"),
        robot_id=robot.id,
        type=SIM_MISSION_TYPE,
        status="QUEUED",
        phase="DISPATCHED",
        progress=0,
        target_pose_snapshot_json=req["route_snapshot"]["waypoints"][-1]["pose"],
        map_id_snapshot=version.map_id,
        map_version_snapshot=version.version,
        semantic_revision_snapshot=manifest["scene_version"],
        parameters_json={
            "mission_id": mission_id,
            "request": req,
            "v21": {
                "vehicle_id": robot.vehicle_id,
                "environment": "SIM",
                "mission_id": mission_id,
                "command_id": command_id,
                "execution_gen": 1,
                "boot_id": robot.boot_id,
                "state": "DISPATCHED",
                "last_seq": None,
                "waypoint_index": None,
                "waypoint_total": len(route["point_refs"]),
                "goal_id": None,
                "goals_used": [],
            },
        },
        created_by=auth.user.id,
    )
    db.add(task)
    write_audit(
        db,
        action="SIM_MISSION_ADMIT",
        resource_type="TASK",
        user_id=auth.user.id,
        resource_id=mission_id,
        after={"scene_digest": manifest["digest"], "vehicle_id": robot.vehicle_id},
        **request_meta(request),
    )
    db.commit()
    return {"task_id": task.id, "mission_request": req}


def _manifest_or_none(db, version: MapVersion) -> dict | None:
    if version.status != "PUBLISHED" or not version.scene_asset_id:
        return None
    doc = json.loads(_verified_asset_bytes(db, version))
    doc.pop("route_arrivals", None)
    return doc


@router.get("/sim-missions/{mission_id}")
def read_sim_mission(
    mission_id: str, db: DbSession, _: AuthContext = Depends(require_permission("robot.read"))
) -> dict:
    from services.sim_v21 import SIM_MISSION_TYPE

    task = db.scalar(
        select(Task).where(
            Task.type == SIM_MISSION_TYPE,
            Task.parameters_json["mission_id"].as_string() == mission_id,
        )
    )
    if task is None:
        raise PlatformError("MISSION_NOT_FOUND", "任务不存在", status_code=404)
    p = task.parameters_json
    return {
        "task_id": task.id,
        "status": task.status,
        "phase": task.phase,
        "progress": task.progress,
        "mission_request": p["request"],
        "state": p["v21"],
        "confirm_code": p.get("confirm_code"),
        "rejected": p.get("rejected", {}),
    }


@router.get("/sim-vehicles/{vehicle_id}/loaded-scene")
def sim_vehicle_scene(
    vehicle_id: str, db: DbSession, auth: AuthContext = Depends(require_permission("robot.read"))
) -> dict:
    """VehicleSnapshot.scene as persisted (confirmed load only counts on the current boot)."""
    from app.modules.robots.router import find_robot
    from services.sim_v21 import scene_state

    robot = find_robot(db, vehicle_id, auth)
    return {**scene_state(robot, db), "latest_load_id": robot.latest_scene_load_id}
