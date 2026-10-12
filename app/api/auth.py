from typing import Optional
from fastapi import Depends, Header, HTTPException, status
from app.config import settings
from app.schemas.auth import AuthenticatedUser


def verify_token_string(token: Optional[str]) -> AuthenticatedUser:
    if not token or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized access: Empty token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token = token.strip()
    if raw_token.startswith("Bearer "):
        raw_token = raw_token[len("Bearer ") :].strip()

    # Server must have tokens configured; reject if all tokens are unset
    if not (settings.ADMIN_TOKEN or settings.PHONE_TOKEN or settings.CAR_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication tokens are not configured on this server.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if settings.ADMIN_TOKEN and raw_token == settings.ADMIN_TOKEN:
        return AuthenticatedUser(identity="admin", role="admin")

    if settings.PHONE_TOKEN and raw_token == settings.PHONE_TOKEN:
        return AuthenticatedUser(identity="phone_client", role="phone")

    if settings.CAR_TOKEN and raw_token == settings.CAR_TOKEN:
        return AuthenticatedUser(
            identity="car_client",
            role="car",
            device_id=settings.DEFAULT_CAR_DEVICE_ID,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized access: Invalid token.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    authorization: Optional[str] = Header(None),
    x_device_token: Optional[str] = Header(None, alias="X-Device-Token"),
) -> AuthenticatedUser:
    token_candidate = None
    if x_device_token and x_device_token.strip():
        token_candidate = x_device_token.strip()
    elif authorization and authorization.startswith("Bearer "):
        token_candidate = authorization[len("Bearer ") :].strip()
    elif authorization and authorization.strip():
        token_candidate = authorization.strip()

    if not token_candidate:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer token authorization.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return verify_token_string(token_candidate)


def require_phone_or_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if user.role not in ["phone", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Phone client or Admin required.",
        )
    return user


def require_car_or_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if user.role not in ["car", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Car client or Admin required.",
        )
    return user
