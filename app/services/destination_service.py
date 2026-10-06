import json
from typing import Optional
import uuid
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from app.config import settings
from app.providers.base import PlaceCandidateItem, SSRFSecurityError
from app.providers.google_maps_parser import resolve_and_parse_map_url
from app.providers.provider_factory import get_maps_provider, get_nlp_provider
from app.repositories.models import Candidate, ParseRequest, Task, utc_now
from app.schemas.destination import (
    CandidateOut,
    DestinationCreateRequest,
    DestinationInputType,
    DestinationQueryResponse,
)
from app.services.task_service import create_task_for_place


def _to_candidate_out(c: Candidate) -> CandidateOut:
    return CandidateOut(
        candidate_id=c.candidate_id,
        title=c.title,
        address=c.address,
        latitude=c.latitude,
        longitude=c.longitude,
        coord_system=c.coord_system,
        source=c.source,
        place_id=c.place_id,
    )


def _build_query_response(req: ParseRequest) -> DestinationQueryResponse:
    return DestinationQueryResponse(
        request_id=req.request_id,
        idempotency_key=req.idempotency_key,
        source=req.source,
        status=req.status,
        transcribed_text=req.transcribed_text,
        candidates=[_to_candidate_out(c) for c in req.candidates],
        clarification_question=req.clarification_question,
        task_id=req.task_id,
        error_message=req.error_message,
        created_at=req.created_at.isoformat(),
    )


async def create_destination_request(
    db: Session,
    req: DestinationCreateRequest,
    caller_id: str,
) -> DestinationQueryResponse:
    # 1. Idempotency Check
    if req.idempotency_key:
        existing = (
            db.query(ParseRequest)
            .filter(
                ParseRequest.idempotency_key == req.idempotency_key,
                ParseRequest.caller_id == caller_id,
            )
            .first()
        )
        if existing:
            # Check payload consistency
            current_payload = req.model_dump_json(exclude={"idempotency_key"})
            if existing.raw_input and existing.raw_input != current_payload:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key collision with differing payload.",
                )
            return _build_query_response(existing)

    request_id = str(uuid.uuid4())
    raw_payload_str = req.model_dump_json(exclude={"idempotency_key"})

    parse_request = ParseRequest(
        request_id=request_id,
        idempotency_key=req.idempotency_key,
        caller_id=caller_id,
        source=req.input_type.value,
        raw_input=raw_payload_str,
        client_lat=req.client_lat,
        client_lon=req.client_lon,
        client_time=req.client_time,
        status="resolving",
    )
    db.add(parse_request)
    db.commit()

    # 2. Process by input type
    target_device = req.target_device_id or settings.DEFAULT_CAR_DEVICE_ID

    if req.input_type == DestinationInputType.COORDS:
        if not req.coordinate:
            parse_request.status = "failed"
            parse_request.error_message = "Missing coordinate in request."
            db.commit()
            raise HTTPException(status_code=400, detail="Missing coordinate.")

        candidate_id = str(uuid.uuid4())
        cand = Candidate(
            candidate_id=candidate_id,
            request_id=request_id,
            title="指定经纬度目的地",
            address=f"经度: {req.coordinate.longitude}, 纬度: {req.coordinate.latitude}",
            latitude=req.coordinate.latitude,
            longitude=req.coordinate.longitude,
            coord_system=req.coordinate.coord_system,
            source="coords",
        )
        db.add(cand)

        # Explicit coordinates directly create task
        task = await create_task_for_place(
            db=db,
            device_id=target_device,
            title=cand.title,
            address=cand.address,
            latitude=cand.latitude,
            longitude=cand.longitude,
            coord_system=cand.coord_system,
            request_id=request_id,
        )
        parse_request.task_id = task.task_id
        parse_request.status = "resolved"
        db.commit()
        db.refresh(parse_request)
        return _build_query_response(parse_request)

    elif req.input_type == DestinationInputType.SHARE_LINK:
        if not req.url:
            parse_request.status = "failed"
            parse_request.error_message = "Missing URL in share_link request."
            db.commit()
            raise HTTPException(status_code=400, detail="Missing URL.")

        try:
            resolved_place = await resolve_and_parse_map_url(req.url)
        except SSRFSecurityError as e:
            parse_request.status = "failed"
            parse_request.error_message = f"Security check failed: {e}"
            db.commit()
            raise HTTPException(status_code=400, detail=f"SSRF security violation: {e}")

        if not resolved_place:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = "未能从该地图链接提取有效目的地坐标，请尝试直接输入地名或地址。"
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        cand = Candidate(
            candidate_id=str(uuid.uuid4()),
            request_id=request_id,
            title=resolved_place.title,
            address=resolved_place.address,
            latitude=resolved_place.latitude,
            longitude=resolved_place.longitude,
            coord_system=resolved_place.coord_system,
            source="share_link",
        )
        db.add(cand)

        # Create task directly for unambiguous map share link
        task = await create_task_for_place(
            db=db,
            device_id=target_device,
            title=cand.title,
            address=cand.address,
            latitude=cand.latitude,
            longitude=cand.longitude,
            coord_system=cand.coord_system,
            request_id=request_id,
        )
        parse_request.task_id = task.task_id
        parse_request.status = "resolved"
        db.commit()
        db.refresh(parse_request)
        return _build_query_response(parse_request)

    elif req.input_type == DestinationInputType.TEXT:
        if not req.text or not req.text.strip():
            parse_request.status = "failed"
            parse_request.error_message = "Empty text input."
            db.commit()
            raise HTTPException(status_code=400, detail="Empty text input.")

        nlp = get_nlp_provider()
        maps = get_maps_provider()

        keywords = await nlp.extract_destinations(req.text)
        if not keywords:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = "未能识别出目的地关键词，请尝试说出具体的建筑物或地名。"
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        all_candidates: list[PlaceCandidateItem] = []
        for kw in keywords:
            results = await maps.search(kw, lat=req.client_lat, lon=req.client_lon)
            all_candidates.extend(results)

        if not all_candidates:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = f"未能找到与“{req.text}”相关的目的地，请补充更明确的地标或区域名称。"
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        for item in all_candidates:
            cand = Candidate(
                candidate_id=str(uuid.uuid4()),
                request_id=request_id,
                title=item.title,
                address=item.address,
                latitude=item.latitude,
                longitude=item.longitude,
                coord_system=item.coord_system,
                source="search",
                place_id=item.place_id,
            )
            db.add(cand)

        if len(all_candidates) == 1:
            parse_request.status = "resolved"
            db.commit()
            db.refresh(parse_request)
            # If exactly 1 clear result from text, can still require or auto confirm; per spec, return candidate for confirmation
            return _build_query_response(parse_request)
        else:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = f"找到 {len(all_candidates)} 个相关地点，请确认具体目的地："
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

    parse_request.status = "failed"
    parse_request.error_message = f"Unsupported input type: {req.input_type}"
    db.commit()
    db.refresh(parse_request)
    return _build_query_response(parse_request)


async def confirm_destination_candidate(
    db: Session,
    request_id: str,
    candidate_id: str,
    target_device_id: str,
) -> Task:
    req = db.query(ParseRequest).filter(ParseRequest.request_id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Destination request not found.")

    # Idempotent re-confirmation: if already resolved and has task_id
    if req.task_id:
        existing_task = db.query(Task).filter(Task.task_id == req.task_id).first()
        if existing_task:
            return existing_task

    cand = (
        db.query(Candidate)
        .filter(Candidate.candidate_id == candidate_id, Candidate.request_id == request_id)
        .first()
    )
    if not cand:
        raise HTTPException(
            status_code=400,
            detail="Candidate does not belong to this request or does not exist.",
        )

    task = await create_task_for_place(
        db=db,
        device_id=target_device_id,
        title=cand.title,
        address=cand.address,
        latitude=cand.latitude,
        longitude=cand.longitude,
        coord_system=cand.coord_system,
        request_id=request_id,
    )

    req.task_id = task.task_id
    req.status = "resolved"
    db.commit()
    return task
