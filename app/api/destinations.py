from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.api.auth import get_current_user, require_phone_or_admin
from app.repositories.database import get_db
from app.repositories.models import ParseRequest
from app.schemas.auth import AuthenticatedUser
from app.schemas.destination import (
    DestinationConfirmRequest,
    DestinationCreateRequest,
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
