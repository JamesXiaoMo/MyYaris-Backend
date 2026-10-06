from fastapi import APIRouter
from app.config import settings
from app.repositories.models import utc_now
from app.schemas.common import HealthResponse

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
def get_health():
    return HealthResponse(
        status="ok",
        version=settings.VERSION,
        timestamp=utc_now().isoformat(),
    )
