from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.dependencies import AuthContext, DbSession, request_meta, require_permission
from app.core.events import append_event
from app.core.serialization import serialize_model
from app.db.models import (
    Asset,
    ExtinguishPoint,
    InspectionPoint,
    Map,
    MapVersion,
    ParkingSlot,
    Site,
    Trajectory,
)

router = APIRouter(prefix="/api/v1", tags=["maps"])


class SiteInput(BaseModel):
    code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str
    timezone: str = "Asia/Shanghai"


class MapInput(BaseModel):
    site_id: str
    code: str
    name: str


class VersionInput(BaseModel):
    map_id: str
    version: str
    semantic_revision: int = 1
    width_m: float = 30
    height_m: float = 20
    origin_x: float = 0
    origin_y: float = 0
    rotation_rad: float = 0
    resolution_m_per_pixel: float = 0.05
    frame_id: str = "map"
    background_asset_id: str | None = None


class SlotInput(BaseModel):
    map_version_id: str
    code: str
    polygon_json: dict[str, Any]
    center_pose_json: dict[str, Any]
    enabled: bool = True


def draft(db, version_id: str) -> MapVersion:
    version = db.get(MapVersion, version_id)
    if not version:
        raise HTTPException(404, "地图版本不存在")
    if version.status != "DRAFT":
        raise HTTPException(409, "Published/Archived 地图不可原地修改，请创建新版本")
    return version


@router.get("/sites")
def list_sites(
    db: DbSession, _: AuthContext = Depends(require_permission("map.read"))
) -> list[dict]:
    return [serialize_model(x) for x in db.scalars(select(Site).order_by(Site.code)).all()]


@router.post("/sites", status_code=201)
def create_site(
    payload: SiteInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    if db.scalar(select(Site).where(Site.code == payload.code)):
        raise HTTPException(409, "Site code 已存在")
    row = Site(**payload.model_dump())
    db.add(row)
    db.flush()
    write_audit(
        db,
        action="SITE_CREATE",
        resource_type="SITE",
        user_id=auth.user.id,
        resource_id=row.id,
        after=payload.model_dump(),
        **request_meta(request),
    )
    db.commit()
    return serialize_model(row)


@router.get("/maps")
def list_maps(
    db: DbSession, _: AuthContext = Depends(require_permission("map.read"))
) -> list[dict]:
    return [serialize_model(x) for x in db.scalars(select(Map).order_by(Map.code)).all()]


@router.post("/maps", status_code=201)
def create_map(
    payload: MapInput, db: DbSession, auth: AuthContext = Depends(require_permission("map.edit"))
) -> dict:
    row = Map(**payload.model_dump())
    db.add(row)
    db.commit()
    return serialize_model(row)


@router.put("/maps/{map_id}")
def update_map(
    map_id: str,
    payload: MapInput,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    row = db.get(Map, map_id)
    if not row:
        raise HTTPException(404, "地图不存在")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return serialize_model(row)


@router.get("/map-versions")
def list_versions(
    db: DbSession,
    map_id: str | None = None,
    _: AuthContext = Depends(require_permission("map.read")),
) -> list[dict]:
    query = select(MapVersion).order_by(MapVersion.created_at.desc())
    if map_id:
        query = query.where(MapVersion.map_id == map_id)
    return [serialize_model(x) for x in db.scalars(query).all()]


@router.post("/map-versions", status_code=201)
def create_version(
    payload: VersionInput,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    row = MapVersion(**payload.model_dump(), status="DRAFT", checksum="", created_by=auth.user.id)
    db.add(row)
    db.commit()
    return serialize_model(row)


@router.post("/map-versions/{version_id}/publish")
def publish_version(
    version_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.publish")),
) -> dict:
    version = draft(db, version_id)
    if not db.scalar(select(ParkingSlot).where(ParkingSlot.map_version_id == version.id)):
        raise HTTPException(409, "发布前至少需要一个车位")
    version.status = "PUBLISHED"
    version.published_at = datetime.now(UTC)
    map_row = db.get(Map, version.map_id)
    if not map_row:
        raise HTTPException(409, "地图版本所属地图不存在")
    map_row.active_version_id = version.id
    write_audit(
        db,
        action="MAP_VERSION_PUBLISH",
        resource_type="MAP_VERSION",
        user_id=auth.user.id,
        resource_id=version.id,
        **request_meta(request),
    )
    db.commit()
    append_event("map.version.published", serialize_model(version))
    return serialize_model(version)


@router.post("/map-versions/{version_id}/archive")
def archive_version(
    version_id: str, db: DbSession, auth: AuthContext = Depends(require_permission("map.publish"))
) -> dict:
    version = db.get(MapVersion, version_id)
    if not version:
        raise HTTPException(404, "地图版本不存在")
    if version.status != "PUBLISHED":
        raise HTTPException(409, "只有 Published 版本可归档")
    version.status = "ARCHIVED"
    db.commit()
    return serialize_model(version)


@router.get("/parking-slots")
def slots(
    db: DbSession,
    map_version_id: str | None = None,
    _: AuthContext = Depends(require_permission("map.read")),
) -> list[dict]:
    query = select(ParkingSlot).order_by(ParkingSlot.code)
    if map_version_id:
        query = query.where(ParkingSlot.map_version_id == map_version_id)
    return [serialize_model(x) for x in db.scalars(query).all()]


@router.post("/parking-slots", status_code=201)
def create_slot(
    payload: SlotInput, db: DbSession, auth: AuthContext = Depends(require_permission("map.edit"))
) -> dict:
    draft(db, payload.map_version_id)
    row = ParkingSlot(**payload.model_dump())
    db.add(row)
    db.commit()
    return serialize_model(row)


@router.put("/parking-slots/{row_id}")
def update_slot(
    row_id: str,
    payload: SlotInput,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    row = db.get(ParkingSlot, row_id)
    if not row:
        raise HTTPException(404, "车位不存在")
    draft(db, row.map_version_id)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return serialize_model(row)


@router.delete("/parking-slots/{row_id}", status_code=204)
def delete_slot(
    row_id: str, db: DbSession, auth: AuthContext = Depends(require_permission("map.edit"))
) -> Response:
    row = db.get(ParkingSlot, row_id)
    if row:
        draft(db, row.map_version_id)
        db.delete(row)
        db.commit()
    return Response(status_code=204)


class InspectionInput(BaseModel):
    map_version_id: str
    parking_slot_id: str
    pose_json: dict[str, Any]
    sensor_orientation_json: dict[str, Any] = Field(default_factory=dict)
    priority: int = 1


class ExtinguishInput(BaseModel):
    map_version_id: str
    parking_slot_id: str
    pose_json: dict[str, Any]
    approach_json: dict[str, Any] = Field(default_factory=dict)
    nozzle_config_json: dict[str, Any] = Field(default_factory=dict)


class TrajectoryInput(BaseModel):
    map_version_id: str
    code: str
    version: str = "1"
    path_json: list[dict[str, Any]]
    enabled: bool = True


def rows_for_version(db, model, version_id: str | None) -> list[dict]:
    query = select(model)
    if version_id:
        query = query.where(model.map_version_id == version_id)
    return [serialize_model(x) for x in db.scalars(query).all()]


@router.get("/inspection-points")
def inspection_points(
    db: DbSession,
    map_version_id: str | None = None,
    _: AuthContext = Depends(require_permission("map.read")),
) -> list[dict]:
    return rows_for_version(db, InspectionPoint, map_version_id)


@router.post("/inspection-points", status_code=201)
def create_inspection(
    payload: InspectionInput,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    draft(db, payload.map_version_id)
    row = InspectionPoint(**payload.model_dump())
    db.add(row)
    db.commit()
    return serialize_model(row)


@router.put("/inspection-points/{row_id}")
def update_inspection(
    row_id: str,
    payload: InspectionInput,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    row = db.get(InspectionPoint, row_id)
    if not row:
        raise HTTPException(404, "巡检点不存在")
    draft(db, row.map_version_id)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return serialize_model(row)


@router.get("/extinguish-points")
def extinguish_points(
    db: DbSession,
    map_version_id: str | None = None,
    _: AuthContext = Depends(require_permission("map.read")),
) -> list[dict]:
    return rows_for_version(db, ExtinguishPoint, map_version_id)


@router.post("/extinguish-points", status_code=201)
def create_extinguish(
    payload: ExtinguishInput,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    draft(db, payload.map_version_id)
    row = ExtinguishPoint(**payload.model_dump())
    db.add(row)
    db.commit()
    return serialize_model(row)


@router.put("/extinguish-points/{row_id}")
def update_extinguish(
    row_id: str,
    payload: ExtinguishInput,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    row = db.get(ExtinguishPoint, row_id)
    if not row:
        raise HTTPException(404, "灭火点不存在")
    draft(db, row.map_version_id)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return serialize_model(row)


@router.get("/trajectories")
def trajectories(
    db: DbSession,
    map_version_id: str | None = None,
    _: AuthContext = Depends(require_permission("map.read")),
) -> list[dict]:
    return rows_for_version(db, Trajectory, map_version_id)


@router.post("/trajectories", status_code=201)
def create_trajectory(
    payload: TrajectoryInput,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    draft(db, payload.map_version_id)
    row = Trajectory(**payload.model_dump())
    db.add(row)
    db.commit()
    return serialize_model(row)


@router.put("/trajectories/{row_id}")
def update_trajectory(
    row_id: str,
    payload: TrajectoryInput,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
) -> dict:
    row = db.get(Trajectory, row_id)
    if not row:
        raise HTTPException(404, "轨迹不存在")
    draft(db, row.map_version_id)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return serialize_model(row)


ALLOWED_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


@router.post("/assets", status_code=201)
async def upload_asset(
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("map.edit")),
    file: UploadFile = File(...),
) -> dict:
    settings = get_settings()
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(415, "只允许 PNG/JPEG/WebP 地图背景")
    original = Path(file.filename or "upload").name
    extension = ALLOWED_MIME[file.content_type]
    accepted_extensions = {extension, ".jpeg"} if extension == ".jpg" else {extension}
    if Path(original).suffix.lower() not in accepted_extensions:
        raise HTTPException(400, "扩展名与 MIME 不一致")
    content = await file.read(settings.upload_max_bytes + 1)
    if len(content) > settings.upload_max_bytes:
        raise HTTPException(413, "文件超过大小限制")
    digest = hashlib.sha256(content).hexdigest()
    object_name = f"{uuid4().hex}{extension}"
    root = settings.asset_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / object_name).resolve()
    if root not in target.parents:
        raise HTTPException(400, "非法文件路径")
    target.write_bytes(content)
    row = Asset(
        object_name=object_name,
        original_filename=original,
        mime_type=file.content_type,
        size_bytes=len(content),
        sha256=digest,
        created_by=auth.user.id,
    )
    db.add(row)
    db.flush()
    write_audit(
        db,
        action="ASSET_UPLOAD",
        resource_type="ASSET",
        user_id=auth.user.id,
        resource_id=row.id,
        after={"sha256": digest, "size": len(content)},
        **request_meta(request),
    )
    db.commit()
    result = serialize_model(row)
    result["url"] = f"/assets/{object_name}"
    return result
