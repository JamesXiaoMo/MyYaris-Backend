from typing import Optional
from fastapi import Depends, Header, HTTPException, status
from app.config import settings
from app.schemas.auth import AuthenticatedUser


def get_current_user(authorization: Optional[str] = Header(None)) -> AuthenticatedUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer token authorization.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[len("Bearer ") :].strip()

    if token == settings.ADMIN_TOKEN:
        return AuthenticatedUser(identity="admin", role="admin")

    if token == settings.PHONE_TOKEN:
        return AuthenticatedUser(identity="phone_client", role="phone")

    if token == settings.CAR_TOKEN:
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
