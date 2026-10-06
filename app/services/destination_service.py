import json
import math
import re
from typing import Optional, Tuple
import uuid
from fastapi import HTTPException, status
from openlocationcode import openlocationcode as olc
from sqlalchemy.orm import Session
from app.config import settings
from app.providers.base import PlaceCandidateItem, ProviderNotConfiguredError, SSRFSecurityError
from app.providers.google_maps_parser import resolve_and_parse_map_url
from app.providers.plus_code_parser import resolve_plus_code
from app.providers.provider_factory import get_maps_provider, get_nlp_provider
from app.repositories.models import Candidate, ParseRequest, Task, utc_now
from app.schemas.destination import (
    CandidateOut,
    DestinationConfirmRequest,
    DestinationCreateRequest,
    DestinationInputType,
    DestinationQueryResponse,
)
from app.services.task_service import create_task_for_place


def parse_strict_coordinates(text: str) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
    """Strictly parse complete latitude, longitude from text.

    Supports:
    - Decimal, positive/negative signs
    - Comma (,), Chinese full-width comma (，), whitespace, newline separators
    - Strictly matches the full string (no partial matches inside messy text or URLs)
    - Validates finite numbers and latitude [-90, 90], longitude [-180, 180]
    - Does NOT guess or swap coordinate order
    """
    if not text or not isinstance(text, str):
        return None, "输入内容为空。"
    cleaned = text.strip()
    pattern = r"^\s*([+-]?\d+(?:\.\d+)?)\s*(?:[,，\s]|\r?\n)\s*([+-]?\d+(?:\.\d+)?)\s*$"
    m = re.match(pattern, cleaned)
    if not m:
        return None, "未能匹配完整经纬度格式。"
    try:
        lat = float(m.group(1))
        lon = float(m.group(2))
    except (ValueError, TypeError):
        return None, "经纬度数值解析失败。"
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None, "经纬度必须为有限数值。"
    if not (-90.0 <= lat <= 90.0):
        return None, f"纬度 {lat} 超出有效范围 [-90, 90]（请确认输入顺序为：纬度在前、经度在后，系统不会自动调换顺序）。"
    if not (-180.0 <= lon <= 180.0):
        return None, f"经度 {lon} 超出有效范围 [-180, 180]。"
    return (lat, lon), None


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
    # Build human-friendly message for client / iPhone Shortcuts
    if req.status == "resolved" and req.task_id:
        if req.candidates:
            cand = req.candidates[0]
            if cand.source == "coords" or "经纬度" in cand.title:
                msg = f"已成功解析坐标 [纬度 {cand.latitude:.6f}, 经度 {cand.longitude:.6f}]，任务已发送到车机待处理。"
            else:
                msg = f"已成功解析「{cand.title}」，任务已发送到车机待处理。"
        else:
            msg = "目的地已解析成功，任务已发送到车机待处理。"
    elif req.status == "needs_clarification":
        msg = req.clarification_question or "输入需要进一步确认，请提供更明确的地点信息。"
    elif req.status == "failed":
        msg = req.error_message or "目的地解析失败，请检查输入格式。"
    else:
        msg = "正在解析处理中。"

    return DestinationQueryResponse(
        request_id=req.request_id,
        idempotency_key=req.idempotency_key,
        source=req.source,
        status=req.status,
        message=msg,
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
    # 1. Semantic Idempotency Check
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
            # Check payload semantic consistency using parsed JSON
            current_dict = json.loads(req.model_dump_json(exclude={"idempotency_key"}))
            try:
                existing_dict = json.loads(existing.raw_input) if existing.raw_input else {}
            except (json.JSONDecodeError, TypeError):
                existing_dict = {}

            # New optional fields must not break retries of requests stored before this release.
            for key in ("plus_code", "reference_coordinate"):
                if key not in existing_dict and current_dict.get(key) is None:
                    current_dict.pop(key, None)
            if existing_dict != current_dict:
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
    target_device = (req.target_device_id or settings.DEFAULT_CAR_DEVICE_ID).strip()
    if not target_device:
        target_device = settings.DEFAULT_CAR_DEVICE_ID

    if req.input_type == DestinationInputType.AUTO:
        raw_text = (req.text or "").strip()
        if not raw_text and req.coordinate:
            coords = (req.coordinate.latitude, req.coordinate.longitude)
            coords_err = None
        else:
            if not raw_text:
                parse_request.status = "failed"
                parse_request.error_message = "Missing text input for auto destination resolution."
                db.commit()
                raise HTTPException(status_code=400, detail="Missing text for auto input.")
            coords, coords_err = parse_strict_coordinates(raw_text)

        # 1. Check if strictly valid coordinates
        if coords is not None:
            lat, lon = coords
            candidate_id = str(uuid.uuid4())
            cand = Candidate(
                candidate_id=candidate_id,
                request_id=request_id,
                title="指定经纬度目的地",
                address=f"经度: {lon:.6f}, 纬度: {lat:.6f}",
                latitude=lat,
                longitude=lon,
                coord_system="wgs84",
                source="coords",
            )
            db.add(cand)

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

        # If coordinates format matched partially but failed range / finiteness check
        if coords_err and ("超出有效范围" in coords_err or "有限数值" in coords_err):
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = coords_err
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        # 2. Check if valid Plus Code format
        norm_val = raw_text.replace("＋", "+").strip()
        code_part = norm_val.split(maxsplit=1)[0].upper()
        if olc.isValid(code_part):
            results = await resolve_plus_code(raw_text, req.reference_coordinate)
            result = results[0]
            if result.error:
                parse_request.status = "failed"
                parse_request.error_message = result.error
            elif result.latitude is None:
                parse_request.status = "needs_clarification"
                parse_request.clarification_question = result.question
            else:
                for item in results:
                    cand = Candidate(
                        candidate_id=str(uuid.uuid4()),
                        request_id=request_id,
                        title=f"Plus Code {item.full_code}",
                        address=item.address or "Plus Code 编码区域中心（并非原始图钉的无限精度坐标）",
                        latitude=item.latitude,
                        longitude=item.longitude,
                        coord_system="wgs84",
                        source="plus_code",
                    )
                    db.add(cand)
                if result.question or len(results) > 1:
                    parse_request.status = "needs_clarification"
                    parse_request.clarification_question = result.question
                else:
                    task = await create_task_for_place(
                        db=db,
                        device_id=target_device,
                        title=cand.title,
                        address=cand.address,
                        latitude=cand.latitude,
                        longitude=cand.longitude,
                        coord_system="wgs84",
                        request_id=request_id,
                    )
                    parse_request.task_id = task.task_id
                    parse_request.status = "resolved"
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        # 3. Neither valid coordinates nor valid Plus Code
        parse_request.status = "needs_clarification"
        parse_request.clarification_question = (
            "未能识别有效目的地。当前统一输入支持明确经纬度坐标（如：34.4245, 132.7083，纬度在前）"
            "或 Plus Code 编码（完整码如 8Q6JCPF8+Q7，短码如 CPF8+Q7 东广岛市 广岛县）。请检查后重新输入。"
        )
        db.commit()
        db.refresh(parse_request)
        return _build_query_response(parse_request)

    elif req.input_type == DestinationInputType.COORDS:
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
            address=f"经度: {req.coordinate.longitude:.6f}, 纬度: {req.coordinate.latitude:.6f}",
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

    elif req.input_type == DestinationInputType.PLUS_CODE:
        results = await resolve_plus_code(req.plus_code or "", req.reference_coordinate)
        result = results[0]
        if result.error:
            parse_request.status = "failed"
            parse_request.error_message = result.error
        elif result.latitude is None:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = result.question
        else:
            for item in results:
                cand = Candidate(
                    candidate_id=str(uuid.uuid4()), request_id=request_id,
                    title=f"Plus Code {item.full_code}",
                    address=item.address or "Plus Code 编码区域中心（并非原始图钉的无限精度坐标）",
                    latitude=item.latitude, longitude=item.longitude,
                    coord_system="wgs84", source="plus_code",
                )
                db.add(cand)
            if result.question or len(results) > 1:
                parse_request.status = "needs_clarification"
                parse_request.clarification_question = result.question
            else:
                task = await create_task_for_place(
                    db=db, device_id=target_device, title=cand.title,
                    address=cand.address, latitude=cand.latitude,
                    longitude=cand.longitude, coord_system="wgs84", request_id=request_id,
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
            resolution = await resolve_and_parse_map_url(req.url)
        except SSRFSecurityError as e:
            parse_request.status = "failed"
            parse_request.error_message = f"Security check failed: {e}"
            db.commit()
            raise HTTPException(status_code=400, detail=f"SSRF security violation: {e}")

        # 1. Error during resolution (e.g. redirect hops exhausted)
        if resolution.error:
            parse_request.status = "failed"
            parse_request.error_message = resolution.error
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        # 2. Unambiguous destination coordinates extracted
        if resolution.candidate:
            cand = Candidate(
                candidate_id=str(uuid.uuid4()),
                request_id=request_id,
                title=resolution.candidate.title,
                address=resolution.candidate.address,
                latitude=resolution.candidate.latitude,
                longitude=resolution.candidate.longitude,
                coord_system=resolution.candidate.coord_system,
                source="share_link",
            )
            db.add(cand)

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

        # 3. Route link without unambiguous destination parameter (avoid guessing origin as destination)
        if resolution.is_route_ambiguous:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = (
                "所分享链接为路线导航链接，且未包含明确终点坐标参数（为避免误将出发地设为终点，系统不予自动猜选）。请直接分享终点地点的图钉链接或明确坐标。"
            )
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        # 4. Multiple distinct coordinates found without explicit destination
        if resolution.has_multiple_locations:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = (
                "所分享链接包含多个地点坐标且无法确认最终目的地。请单独分享目的地图钉或直接输入明确经纬度。"
            )
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        # 5. Viewport-only center URL (camera position, not destination)
        if resolution.is_viewport_only:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = (
                "所分享链接仅为地图视野中心，并非具体的目的地或图钉。请在地图上长按放置图钉或点击具体地点后再分享。"
            )
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        # 6. Place name extracted from URL but no coordinates
        if resolution.place_name:
            try:
                maps = get_maps_provider()
                search_results = await maps.search(resolution.place_name, lat=req.client_lat, lon=req.client_lon)
                if search_results:
                    for item in search_results:
                        cand = Candidate(
                            candidate_id=str(uuid.uuid4()),
                            request_id=request_id,
                            title=item.title,
                            address=item.address,
                            latitude=item.latitude,
                            longitude=item.longitude,
                            coord_system=item.coord_system,
                            source=item.source or "share_link_search",
                            place_id=item.place_id,
                        )
                        db.add(cand)
                    parse_request.status = "needs_clarification"
                    parse_request.clarification_question = (
                        f"从链接中识别到地点“{resolution.place_name}”，检索到以下候选地点（均为地区概略范围/非建筑物精确图钉）。请确认具体目的地后下发导航，或直接输入精确经纬度："
                    )
                    db.commit()
                    db.refresh(parse_request)
                    return _build_query_response(parse_request)
                else:
                    parse_request.status = "needs_clarification"
                    parse_request.clarification_question = (
                        f"已从地图链接识别地点“{resolution.place_name}”，但地图服务未检索到匹配结果（可能缺少详细街道番地）。请尝试补充更详细的门牌地址，或在地图上长按图钉分享明确坐标。"
                    )
                    db.commit()
                    db.refresh(parse_request)
                    return _build_query_response(parse_request)
            except ProviderNotConfiguredError:
                parse_request.status = "needs_clarification"
                parse_request.clarification_question = (
                    f"已从地图链接识别地点“{resolution.place_name}”，但该链接未包含精确图钉坐标且地图搜索服务未配置。请直接输入经纬度或提供包含图钉坐标的分享链接。"
                )
                db.commit()
                db.refresh(parse_request)
                return _build_query_response(parse_request)

        # 7. Fallback when neither coordinates nor place name could be resolved
        parse_request.status = "needs_clarification"
        parse_request.clarification_question = (
            "未能从该地图链接提取有效目的地坐标。请尝试直接在地图上放置图钉后分享，或输入明确经纬度。"
        )
        db.commit()
        db.refresh(parse_request)
        return _build_query_response(parse_request)

    elif req.input_type == DestinationInputType.TEXT:
        if not req.text or not req.text.strip():
            parse_request.status = "failed"
            parse_request.error_message = "Empty text input."
            db.commit()
            raise HTTPException(status_code=400, detail="Empty text input.")

        # Maps provider is required for place searches
        try:
            maps = get_maps_provider()
        except ProviderNotConfiguredError as e:
            parse_request.status = "failed"
            parse_request.error_message = f"地图检索服务未配置: {e}"
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        # NLP is optional: use extracted keywords if configured, otherwise search raw text directly
        keywords: list[str] = []
        try:
            nlp = get_nlp_provider()
            keywords = await nlp.extract_destinations(req.text)
        except ProviderNotConfiguredError:
            keywords = [req.text.strip()]

        if not keywords:
            keywords = [req.text.strip()]

        all_candidates: list[PlaceCandidateItem] = []
        for kw in keywords[:3]:
            results = await maps.search(kw, lat=req.client_lat, lon=req.client_lon)
            all_candidates.extend(results)

        if not all_candidates:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = f"未能找到与“{req.text}”相关的目的地，请补充更明确的地标或街道门牌地址。"
            db.commit()
            db.refresh(parse_request)
            return _build_query_response(parse_request)

        for item in all_candidates[:3]:
            cand = Candidate(
                candidate_id=str(uuid.uuid4()),
                request_id=request_id,
                title=item.title,
                address=item.address,
                latitude=item.latitude,
                longitude=item.longitude,
                coord_system=item.coord_system,
                source=item.source or "search",
                place_id=item.place_id,
            )
            db.add(cand)

        # All place/address search results MUST require user confirmation (never auto-dispatch regional centroids)
        parse_request.status = "needs_clarification"
        parse_request.clarification_question = f"找到 {len(all_candidates[:3])} 个相关候选（均为概略范围/非建筑物精确图钉），请确认具体目的地："
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
