from typing import Optional
from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session
from app.api.auth import get_current_user
from app.repositories.database import get_db
from app.schemas.auth import AuthenticatedUser
from app.schemas.voice import VoiceUploadResponse
from app.services.device_service import resolve_car_device_id
from app.services.voice_service import process_voice_upload

router = APIRouter(tags=["Voice"])


@router.post("/voice", response_model=VoiceUploadResponse)
async def upload_voice(
    file: UploadFile = File(...),
    idempotency_key: Optional[str] = Form(None),
    client_lat: Optional[float] = Form(None),
    client_lon: Optional[float] = Form(None),
    client_time: Optional[str] = Form(None),
    target_device_id: Optional[str] = Form("car_p3pro"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    norm_target = resolve_car_device_id(target_device_id)
    return await process_voice_upload(
        db=db,
        file=file,
        caller_id=user.identity,
        idempotency_key=idempotency_key,
        client_lat=client_lat,
        client_lon=client_lon,
        client_time=client_time,
        target_device_id=norm_target,
    )
