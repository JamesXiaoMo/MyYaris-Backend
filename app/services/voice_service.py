from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
import uuid
from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session
from app.config import settings
from app.providers.provider_factory import get_asr_provider, get_maps_provider, get_nlp_provider
from app.repositories.models import Candidate, ParseRequest
from app.schemas.voice import VoiceUploadResponse


async def process_voice_upload(
    db: Session,
    file: UploadFile,
    caller_id: str,
    idempotency_key: str | None = None,
    client_lat: float | None = None,
    client_lon: float | None = None,
    client_time: str | None = None,
    target_device_id: str | None = None,
) -> VoiceUploadResponse:
    # Check idempotency
    if idempotency_key:
        existing = (
            db.query(ParseRequest)
            .filter(
                ParseRequest.idempotency_key == idempotency_key,
                ParseRequest.caller_id == caller_id,
            )
            .first()
        )
        if existing:
            return VoiceUploadResponse(
                request_id=existing.request_id,
                status=existing.status,
                message="Idempotent voice request returned existing result",
            )

    request_id = str(uuid.uuid4())
    temp_dir = Path(settings.AUDIO_TEMP_DIR)
    temp_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename or "audio.wav").suffix or ".wav"
    temp_audio_path = temp_dir / f"{request_id}{ext}"

    content = await file.read()
    if len(content) > settings.MAX_AUDIO_FILE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Audio file exceeds allowed limit.")

    with open(temp_audio_path, "wb") as f:
        f.write(content)

    parse_request = ParseRequest(
        request_id=request_id,
        idempotency_key=idempotency_key,
        caller_id=caller_id,
        source="voice",
        raw_input=f"audio_file:{temp_audio_path.name}",
        client_lat=client_lat,
        client_lon=client_lon,
        client_time=client_time,
        status="resolving",
    )
    db.add(parse_request)
    db.commit()

    # Transcribe and parse
    try:
        asr = get_asr_provider()
        nlp = get_nlp_provider()
        maps = get_maps_provider()

        text = await asr.transcribe(str(temp_audio_path))
        parse_request.transcribed_text = text

        dest_keywords = await nlp.extract_destinations(text)
        if not dest_keywords:
            parse_request.status = "needs_clarification"
            parse_request.clarification_question = f"已识别语音为“{text}”，但未识别出具体目的地，请补充地名。"
        else:
            all_cands = []
            for kw in dest_keywords:
                cands = await maps.search(kw, lat=client_lat, lon=client_lon)
                all_cands.extend(cands)

            if not all_cands:
                parse_request.status = "needs_clarification"
                parse_request.clarification_question = f"已识别为“{text}”，但未能检索到相关地点，请确认地名。"
            else:
                for item in all_cands:
                    c = Candidate(
                        candidate_id=str(uuid.uuid4()),
                        request_id=request_id,
                        title=item.title,
                        address=item.address,
                        latitude=item.latitude,
                        longitude=item.longitude,
                        coord_system=item.coord_system,
                        source="voice_search",
                        place_id=item.place_id,
                    )
                    db.add(c)
                
                if len(all_cands) == 1:
                    parse_request.status = "resolved"
                else:
                    parse_request.status = "needs_clarification"
                    parse_request.clarification_question = f"从语音中找到 {len(all_cands)} 个可能地点，请选择确认："
    except Exception as e:
        parse_request.status = "failed"
        parse_request.error_message = f"Voice processing failed: {e}"
    finally:
        # Clean up temporary audio file immediately after processing
        if temp_audio_path.exists():
            try:
                temp_audio_path.unlink()
            except OSError:
                pass

    db.commit()
    db.refresh(parse_request)
    return VoiceUploadResponse(
        request_id=parse_request.request_id,
        status=parse_request.status,
        message="Voice uploaded and processed successfully",
    )


def clean_stale_audio_files() -> int:
    temp_dir = Path(settings.AUDIO_TEMP_DIR)
    if not temp_dir.exists():
        return 0
    now = time.time()
    cutoff = now - (settings.AUDIO_RETENTION_HOURS * 3600)
    cleaned = 0
    for f in temp_dir.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                cleaned += 1
            except OSError:
                pass
    return cleaned
