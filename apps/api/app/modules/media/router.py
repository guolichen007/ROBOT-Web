from fastapi import APIRouter
from sqlalchemy import select

from app.core.dependencies import CurrentAuth, DbSession
from app.core.serialization import serialize_model
from app.db.models import StreamRegistry

router = APIRouter(prefix="/api/v1/media", tags=["media"])


@router.get("/streams")
def streams(db: DbSession, auth: CurrentAuth, robot_id: str | None = None) -> list[dict]:
    query = select(StreamRegistry).order_by(StreamRegistry.camera_type)
    if robot_id:
        query = query.where(StreamRegistry.robot_id == robot_id)
    return [serialize_model(x) for x in db.scalars(query).all()]
