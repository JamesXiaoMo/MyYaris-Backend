from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.api.auth import get_current_user, require_phone_or_admin
from app.repositories.database import get_db
from app.repositories.models import ParseRequest
from app.schemas.auth import AuthenticatedUser
from app.schemas.destination import (
    DestinationConfirmRequest,
    DestinationCreateRequest,
    DestinationInputType,
    DestinationQueryResponse,
)
from app.schemas.task import TaskOut
from app.services.destination_service import (
    _build_query_response,
    confirm_destination_candidate,
    create_destination_request,
)

router = APIRouter(prefix="/destinations", tags=["Destinations"])


@router.post("", response_model=DestinationQueryResponse, status_code=status.HTTP_200_OK)
async def submit_destination(
    req: DestinationCreateRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    return await create_destination_request(db, req, caller_id=user.identity)


@router.get("/{id}", response_model=DestinationQueryResponse)
def get_destination(
    id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    req = db.query(ParseRequest).filter(ParseRequest.request_id == id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Destination request not found.")
    return _build_query_response(req)


@router.post("/{id}/confirm", response_model=TaskOut)
async def confirm_destination(
    id: str,
    body: DestinationConfirmRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    task = await confirm_destination_candidate(
        db=db,
        request_id=id,
        candidate_id=body.candidate_id,
        target_device_id=body.target_device_id or "car_p3pro",
    )
    return TaskOut(
        task_id=task.task_id,
        device_id=task.device_id,
        title=task.title,
        address=task.address,
        latitude=task.latitude,
        longitude=task.longitude,
        coord_system=task.coord_system,
        status=task.status,
        created_at=task.created_at.isoformat(),
        expires_at=task.expires_at.isoformat(),
        delivery_attempts=task.delivery_attempts,
        error_message=task.error_message,
    )


# --- Standard V2.0 /api/v1 Navigation Endpoints (NAV-01~03) ---

from typing import Optional
from pydantic import BaseModel, Field

api_v1_router = APIRouter(prefix="/api/v1", tags=["Navigation V1 Standard"])


class DestinationResolveRequest(BaseModel):
    raw_input: str
    source: Optional[str] = "ios_share"


class DestinationResolveResponse(BaseModel):
    code: int = 200
    destination_id: str
    name: str
    latitude: float
    longitude: float
    confidence: float = 1.0


class TaskDestinationRequest(BaseModel):
    destination_id: str
    expires_in_seconds: Optional[int] = 3600
    target_device_id: Optional[str] = None


@api_v1_router.post("/destinations/resolve", response_model=DestinationResolveResponse)
async def resolve_destination_v1(
    body: DestinationResolveRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    if body.raw_input.startswith("http://") or body.raw_input.startswith("https://"):
        create_req = DestinationCreateRequest(
            input_type=DestinationInputType.SHARE_LINK,
            url=body.raw_input,
        )
    else:
        create_req = DestinationCreateRequest(
            input_type=DestinationInputType.AUTO,
            text=body.raw_input,
        )
    query_resp = await create_destination_request(db, create_req, caller_id=user.identity)

    if query_resp.candidates:
        c = query_resp.candidates[0]
        return DestinationResolveResponse(
            code=200,
            destination_id=query_resp.request_id,
            name=c.title,
            latitude=c.latitude,
            longitude=c.longitude,
            confidence=0.98,
        )
    else:
        return DestinationResolveResponse(
            code=200,
            destination_id=query_resp.request_id,
            name=body.raw_input,
            latitude=0.0,
            longitude=0.0,
            confidence=0.5,
        )


@api_v1_router.post("/tasks/destination", response_model=TaskOut)
async def create_task_from_destination_v1(
    body: TaskDestinationRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    req = db.query(ParseRequest).filter(ParseRequest.request_id == body.destination_id).first()
    if not req or not req.candidates:
        raise HTTPException(status_code=404, detail="Destination or candidates not found.")

    cand_id = req.candidates[0].candidate_id
    dev_id = body.target_device_id or "car_p3pro"

    task = await confirm_destination_candidate(
        db=db,
        request_id=body.destination_id,
        candidate_id=cand_id,
        target_device_id=dev_id,
    )
    return TaskOut(
        task_id=task.task_id,
        device_id=task.device_id,
        title=task.title,
        address=task.address,
        latitude=task.latitude,
        longitude=task.longitude,
        coord_system=task.coord_system,
        status=task.status,
        created_at=task.created_at.isoformat(),
        expires_at=task.expires_at.isoformat(),
        delivery_attempts=task.delivery_attempts,
        error_message=task.error_message,
    )
