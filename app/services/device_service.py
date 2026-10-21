from typing import Optional
from app.config import settings

KNOWN_CAR_ALIASES = {"car_p3pro", "ottocast_p3_pro"}


def resolve_car_device_id(input_id: Optional[str] = None, fallback_id: Optional[str] = None) -> str:
    """
    Normalizes vehicle device IDs across navigation, music control, and polling APIs.
    Maps known aliases (e.g. 'ottocast_p3_pro') to the canonical device ID ('car_p3pro').
    If neither input_id nor fallback_id is provided, defaults to settings.DEFAULT_CAR_DEVICE_ID.
    """
    candidate = input_id or fallback_id or settings.DEFAULT_CAR_DEVICE_ID or "car_p3pro"
    candidate_str = candidate.strip()
    if candidate_str in KNOWN_CAR_ALIASES:
        return settings.DEFAULT_CAR_DEVICE_ID or "car_p3pro"
    return candidate_str


def is_same_device(device_a: Optional[str], device_b: Optional[str]) -> bool:
    """
    Checks if two device IDs refer to the same logical device after alias normalization.
    """
    if not device_a or not device_b:
        return False
    return resolve_car_device_id(device_a) == resolve_car_device_id(device_b)


# iPhone-local playback sessions live in their own queue scope, never in a car device's scope.
LOCAL_SCOPE_PREFIX = "local~"


def is_local_scope(device_id: Optional[str]) -> bool:
    return bool(device_id) and device_id.strip().startswith(LOCAL_SCOPE_PREFIX)
