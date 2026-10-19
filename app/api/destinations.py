from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.api.auth import get_current_user, require_phone_or_admin
from app.repositories.database import get_db
from app.repositories.models import ParseRequest, Candidate
from app.schemas.auth import AuthenticatedUser
from app.schemas.destination import (
    DestinationConfirmRequest,
    DestinationCreateRequest,
    DestinationInputType,
    DestinationQueryResponse,
)
from app.schemas.task import TaskOut
from app.services.device_service import resolve_car_device_id
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
    if user.role == "user" and not user.has_device_control:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: User account lacks vehicle device control permission.",
        )

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
    req = db.query(ParseRequest).filter(ParseRequest.request_id == id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Destination request not found.")
    if req.source in ("favorite_preview", "favorite"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该解析请求仅用于收藏预览确认，禁止调用旧版 confirm 接口创建车机导航任务。如需保存请调用收藏夹接口。",
        )
    task = await confirm_destination_candidate(
        db=db,
        request_id=id,
        candidate_id=body.candidate_id,
        target_device_id=resolve_car_device_id(body.target_device_id),
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

import math
import uuid
from typing import List, Optional
from openlocationcode import openlocationcode as olc
from pydantic import BaseModel, Field

from app.schemas.common import Coordinate
from app.schemas.destination import CandidateOut

api_v1_router = APIRouter(prefix="/api/v1", tags=["Navigation V1 Standard"])


class DestinationResolveRequest(BaseModel):
    raw_input: str
    source: Optional[str] = "ios_share"
    reference_coordinate: Optional[Coordinate] = None
    purpose: Optional[str] = Field(
        default="favorite_preview",
        description="Resolution purpose: 'favorite_preview' (strict offline WGS84 short-code rules, cannot be used for legacy confirm dispatch) or 'navigation'",
    )


class DestinationResolveResponse(BaseModel):
    code: int = 200
    destination_id: str
    name: str
    latitude: float
    longitude: float
    confidence: float = 1.0
    status: Optional[str] = "resolved"
    message: Optional[str] = None
    clarification_question: Optional[str] = None
    needs_confirmation: bool = False
    candidates: List[CandidateOut] = []


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
    raw_text = (body.raw_input or "").strip()
    norm_val = raw_text.replace("＋", "+").strip()
    code_part = norm_val.split(maxsplit=1)[0].upper()

    # 1. Plus Code offline resolution branch
    if olc.isValid(code_part):
        # 1a. Short Plus Code: strictly requires explicit valid WGS84 reference_coordinate
        if olc.isShort(code_part):
            if body.reference_coordinate is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="短 Plus Code 缺少参照坐标，必须显式提供合法的 WGS84 参考经纬度 (reference_coordinate)，不支持仅凭城市名猜测。",
                )
            ref = body.reference_coordinate
            ref_sys = (ref.coord_system or "").lower()
            if ref_sys != "wgs84":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="短编码参照坐标系统必须为 wgs84。",
                )
            if not (math.isfinite(ref.latitude) and math.isfinite(ref.longitude)):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="短编码参照坐标经纬度必须为有效有限数值。",
                )
            if not (-90.0 <= ref.latitude <= 90.0 and -180.0 <= ref.longitude <= 180.0):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="短编码参照坐标经纬度超出合法范围。",
                )

            # Offline recovery via WGS84 reference coordinate
            recovered_full = olc.recoverNearest(code_part, ref.latitude, ref.longitude)
            area = olc.decode(recovered_full)
            center_lat = area.latitudeCenter
            center_lon = area.longitudeCenter
            title = f"Plus Code {recovered_full}"
            address = "Plus Code 编码区域中心（按参照位置还原，非建筑物入口精确坐标）"
            clarify_q = "已按参照位置还原最近匹配的短编码区域中心，请确认是否为目标地点；参照位置偏差可能导致错误地点。"

            req_id = str(uuid.uuid4())
            cand_id = str(uuid.uuid4())
            parse_request = ParseRequest(
                request_id=req_id,
                caller_id=user.identity,
                source=body.purpose or "favorite_preview",
                raw_input=raw_text,
                status="needs_clarification",
                clarification_question=clarify_q,
            )
            cand = Candidate(
                candidate_id=cand_id,
                request_id=req_id,
                title=title,
                address=address,
                latitude=center_lat,
                longitude=center_lon,
                coord_system="wgs84",
                source="plus_code",
            )
            db.add(parse_request)
            db.add(cand)
            db.commit()

            cand_out = CandidateOut(
                candidate_id=cand_id,
                title=title,
                address=address,
                latitude=center_lat,
                longitude=center_lon,
                coord_system="wgs84",
                source="plus_code",
            )
            return DestinationResolveResponse(
                code=200,
                destination_id=req_id,
                name=title,
                latitude=center_lat,
                longitude=center_lon,
                confidence=0.85,
                status="needs_clarification",
                message=None,
                clarification_question=clarify_q,
                needs_confirmation=True,
                candidates=[cand_out],
            )

        # 1b. Full Plus Code offline decoding
        elif olc.isFull(code_part):
            area = olc.decode(code_part)
            center_lat = area.latitudeCenter
            center_lon = area.longitudeCenter
            is_coarse = (area.codeLength < 10)
            status_val = "needs_clarification" if is_coarse else "resolved"
            clarify_q = (
                "该编码覆盖较大区域，取区域网格几何中心位置，请确认是否为目标地点，或提供更长更精确的 Plus Code。"
                if is_coarse else None
            )
            confidence = 0.85 if is_coarse else 0.98
            title = f"Plus Code {code_part}"
            address = "Plus Code 编码区域中心（几何网格中心，非建筑物入口精确坐标）"

            req_id = str(uuid.uuid4())
            cand_id = str(uuid.uuid4())
            parse_request = ParseRequest(
                request_id=req_id,
                caller_id=user.identity,
                source=body.purpose or "favorite_preview",
                raw_input=raw_text,
                status=status_val,
                clarification_question=clarify_q,
            )
            cand = Candidate(
                candidate_id=cand_id,
                request_id=req_id,
                title=title,
                address=address,
                latitude=center_lat,
                longitude=center_lon,
                coord_system="wgs84",
                source="plus_code",
            )
            db.add(parse_request)
            db.add(cand)
            db.commit()

            cand_out = CandidateOut(
                candidate_id=cand_id,
                title=title,
                address=address,
                latitude=center_lat,
                longitude=center_lon,
                coord_system="wgs84",
                source="plus_code",
            )
            return DestinationResolveResponse(
                code=200,
                destination_id=req_id,
                name=title,
                latitude=center_lat,
                longitude=center_lon,
                confidence=confidence,
                status=status_val,
                message=None,
                clarification_question=clarify_q,
                needs_confirmation=is_coarse,
                candidates=[cand_out],
            )

    # 2. Reference coordinate check for other input types if provided
    if body.reference_coordinate is not None:
        ref_sys = (body.reference_coordinate.coord_system or "").lower()
        if ref_sys != "wgs84":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="参照坐标系统必须为 wgs84。",
            )

    # 3. Share link and Auto destination parsing
    if raw_text.startswith("http://") or raw_text.startswith("https://"):
        create_req = DestinationCreateRequest(
            input_type=DestinationInputType.SHARE_LINK,
            url=raw_text,
            reference_coordinate=body.reference_coordinate,
            auto_create_task=False,
            purpose=body.purpose or "favorite_preview",
        )
    else:
        create_req = DestinationCreateRequest(
            input_type=DestinationInputType.AUTO,
            text=raw_text,
            reference_coordinate=body.reference_coordinate,
            auto_create_task=False,
            purpose=body.purpose or "favorite_preview",
        )
    query_resp = await create_destination_request(db, create_req, caller_id=user.identity)

    if query_resp.candidates:
        c = query_resp.candidates[0]
        has_multi = len(query_resp.candidates) > 1
        is_clarify = (query_resp.status == "needs_clarification") or has_multi
        clarify_q = (
            query_resp.clarification_question
            or (f"找到 {len(query_resp.candidates)} 个候选地点，请确认具体目标地点。" if has_multi else None)
        )
        return DestinationResolveResponse(
            code=200,
            destination_id=query_resp.request_id,
            name=c.title,
            latitude=c.latitude,
            longitude=c.longitude,
            confidence=0.98 if not is_clarify else 0.85,
            status="needs_clarification" if is_clarify else "resolved",
            message=query_resp.message,
            clarification_question=clarify_q,
            needs_confirmation=is_clarify,
            candidates=query_resp.candidates,
        )
    else:
        # Never fill (0.0, 0.0) as default/missing coordinates.
        error_detail = (
            query_resp.clarification_question
            or query_resp.error_message
            or f"无法识别或解析目的地: {body.raw_input}"
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail,
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
    if req.status == "needs_clarification":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该目的地尚需用户确认或澄清，无法直接派发导航任务。",
        )

    cand_id = req.candidates[0].candidate_id
    dev_id = resolve_car_device_id(body.target_device_id)

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
