import os
from pathlib import Path
import secrets
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.database import SessionLocal, get_db
from app.repositories.models import User
from app.schemas.auth import (
    AuthenticatedUser,
    BootstrapAdminRequest,
    ChangePasswordRequest,
    DeviceBindingRequest,
    LoginResponse,
    UpdateProfileRequest,
    UserListResponse,
    UserLoginRequest,
    UserRegisterRequest,
    UserResponse,
    UserRoleUpdateRequest,
)
from app.services.auth_service import (
    auth_service,
    has_user_device_control,
    user_to_response,
)

router = APIRouter(prefix="/api/v1/auth", tags=["User Authentication & Accounts"])


# ---------------------------------------------------------------------------
# Authentication Dependencies
# ---------------------------------------------------------------------------

def verify_token_string(token: Optional[str], db: Optional[Session] = None) -> AuthenticatedUser:
    """
    Validates a token candidate. Supports:
    1. Dynamic server-side user sessions (created upon login)
    2. Legacy static pre-shared tokens (transitional compatibility)
    """
    if not token or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized access: Empty token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_token = token.strip()
    if raw_token.startswith("Bearer "):
        raw_token = raw_token[len("Bearer ") :].strip()

    # 1. Try resolving as a dynamic user session in database
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    try:
        session_result = auth_service.validate_session(db, raw_token)
        if session_result:
            user, session = session_result
            can_control = (user.role == "admin") or has_user_device_control(db, user.id)
            return AuthenticatedUser(
                identity=user.username,
                role=user.role,
                user_id=user.id,
                username=user.username,
                is_legacy_token=False,
                has_device_control=can_control,
            )
    finally:
        if close_db:
            db.close()

    # 2. Fallback to transitional legacy pre-shared tokens (if enabled)
    if settings.ENABLE_LEGACY_TOKEN_AUTH:
        if settings.ADMIN_TOKEN and raw_token == settings.ADMIN_TOKEN:
            return AuthenticatedUser(
                identity="admin",
                role="admin",
                is_legacy_token=True,
                has_device_control=True,
            )

        if settings.PHONE_TOKEN and raw_token == settings.PHONE_TOKEN:
            return AuthenticatedUser(
                identity="phone_client",
                role="phone",
                is_legacy_token=True,
                has_device_control=True,
            )

        if settings.CAR_TOKEN and raw_token == settings.CAR_TOKEN:
            return AuthenticatedUser(
                identity="car_client",
                role="car",
                device_id=settings.DEFAULT_CAR_DEVICE_ID,
                is_legacy_token=True,
                has_device_control=True,
            )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized access: Invalid or expired credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_device_token: Optional[str] = Header(None, alias="X-Device-Token"),
    db: Session = Depends(get_db),
) -> AuthenticatedUser:
    """
    Extracts authentication credentials from:
    1. HttpOnly Session Cookie (`myyaris_session`)
    2. Authorization Header (`Bearer <token>`)
    3. X-Device-Token Header
    """
    token_candidate = None

    # Priority 1: HttpOnly Session Cookie
    cookie_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if cookie_token and cookie_token.strip():
        token_candidate = cookie_token.strip()

    # Priority 2: X-Device-Token Header
    elif x_device_token and x_device_token.strip():
        token_candidate = x_device_token.strip()

    # Priority 3: Authorization Header
    elif authorization and authorization.strip():
        raw_auth = authorization.strip()
        if raw_auth.startswith("Bearer "):
            token_candidate = raw_auth[len("Bearer ") :].strip()
        else:
            token_candidate = raw_auth

    if not token_candidate:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials: provide session cookie or Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return verify_token_string(token_candidate, db=db)


def require_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Administrator privileges required.",
        )
    return user


def require_admin_or_legacy_phone(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """
    Guards global service configurations like NetEase account credentials.
    - Admin: permitted.
    - Legacy phone token: permitted for transitional compatibility.
    - Registered standard user: strictly forbidden (403).
    """
    if user.role == "admin" or (user.is_legacy_token and user.role == "phone"):
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access forbidden: Administrator privileges required.",
    )


def require_user_or_phone_or_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """
    Guards user features like favorites.
    - Registered user ('user'): permitted.
    - Legacy phone token ('phone'): permitted.
    - Admin: permitted.
    - Car device ('car'): strictly forbidden (403).
    """
    if user.role in ["user", "phone", "admin"]:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access forbidden: Car clients cannot access user favorites.",
    )


def require_phone_or_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """
    Guards phone-level operations and vehicle device control.
    - Admin: always permitted.
    - Legacy phone token: permitted during transitional period.
    - Registered user: permitted ONLY if user has explicit device control permission.
    """
    if user.role == "admin" or user.role == "phone":
        return user

    if user.role == "user":
        if not user.has_device_control:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access forbidden: User account lacks vehicle device control permission. Contact administrator for device binding.",
            )
        return user

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Access forbidden: Phone client, authorized user, or Admin required.",
    )


def require_car_or_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if user.role not in ["car", "admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Vehicle device or Admin required.",
        )
    return user


# ---------------------------------------------------------------------------
# Account & Auth Routes
# ---------------------------------------------------------------------------

@router.post("/bootstrap-admin", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def bootstrap_first_admin(
    req: BootstrapAdminRequest,
    db: Session = Depends(get_db),
):
    """
    Securely bootstraps the initial system administrator.
    Only permitted when the database has ZERO registered users.
    Once an administrator exists, this endpoint is permanently closed (403 Forbidden).
    """
    try:
        admin_user = auth_service.bootstrap_admin(db, req)
        return user_to_response(db, admin_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    req: UserRegisterRequest,
    db: Session = Depends(get_db),
):
    """
    Registers a new standard user account.
    - Role is strictly 'user'.
    - Vehicle control permission is disabled by default (requires admin binding).
    - Password is securely hashed using Argon2id.
    """
    try:
        user = auth_service.register_user(db, req)
        return user_to_response(db, user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/login", response_model=LoginResponse)
def login(
    req: UserLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    Authenticates user credentials.
    - Mitigates user enumeration by returning identical generic error messages on failure.
    - Implements account lockout after excessive failed attempts.
    - Sets HttpOnly, Secure, SameSite=Lax session cookie on successful login.
    - Returns dynamic internal session_id for non-browser clients (Bearer token).
    """
    user_agent = request.headers.get("user-agent")
    client_ip = request.client.host if request.client else None

    try:
        user, session = auth_service.authenticate_user(
            db,
            username=req.username,
            password=req.password,
            user_agent=user_agent,
            ip_address=client_ip,
        )
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Set secure revocable session cookie
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=session.session_id,
        max_age=settings.SESSION_EXPIRE_DAYS * 86400,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/",
    )

    return LoginResponse(
        code=200,
        message="Login successful",
        session_id=session.session_id,
        token_type="bearer",
        expires_at=session.expires_at.isoformat(),
        user=user_to_response(db, user),
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user_auth: AuthenticatedUser = Depends(get_current_user),
):
    """
    Logs out the current session.
    Revokes the session in SQLite database and deletes the session cookie.
    """
    # 1. Clear session cookie
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/",
    )

    # 2. Revoke session in database
    cookie_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    auth_header = request.headers.get("authorization", "")
    session_id = cookie_token
    if not session_id and auth_header.startswith("Bearer "):
        session_id = auth_header[len("Bearer ") :].strip()

    if session_id:
        auth_service.revoke_session(db, session_id)

    return {"code": 200, "message": "Successfully logged out."}


@router.get("/me", response_model=UserResponse)
def get_current_user_profile(
    db: Session = Depends(get_db),
    user_auth: AuthenticatedUser = Depends(get_current_user),
):
    """
    Returns profile information for the authenticated caller.
    Sensitive fields (password hash, internal session tokens) are excluded.
    """
    if user_auth.user_id:
        user = db.query(User).filter(User.id == user_auth.user_id).first()
        if user:
            return user_to_response(db, user)

    # Fallback response for legacy tokens
    return UserResponse(
        id=user_auth.user_id or f"legacy_{user_auth.identity}",
        username=user_auth.username or user_auth.identity,
        nickname=user_auth.identity,
        avatar_url=None,
        role=user_auth.role,
        is_active=True,
        has_device_control=user_auth.has_device_control,
        created_at="",
        updated_at="",
        last_login_at=None,
    )


@router.patch("/profile", response_model=UserResponse)
def update_profile(
    req: UpdateProfileRequest,
    db: Session = Depends(get_db),
    user_auth: AuthenticatedUser = Depends(get_current_user),
):
    """Updates current user profile with safe avatar URL validation (SSRF protected)."""
    if not user_auth.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Profile update is only supported for registered user accounts.",
        )

    user = db.query(User).filter(User.id == user_auth.user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    try:
        updated = auth_service.update_profile(db, user, req)
        return user_to_response(db, updated)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/change-password")
def change_password(
    req: ChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    user_auth: AuthenticatedUser = Depends(get_current_user),
):
    """
    Changes user password.
    Requires confirming the old password. Revokes all other sessions on success.
    """
    if not user_auth.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password change is only supported for registered user accounts.",
        )

    user = db.query(User).filter(User.id == user_auth.user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    cookie_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    auth_header = request.headers.get("authorization", "")
    current_session = cookie_token
    if not current_session and auth_header.startswith("Bearer "):
        current_session = auth_header[len("Bearer ") :].strip()

    try:
        auth_service.change_password(db, user, req, current_session_id=current_session)
        return {"code": 200, "message": "Password changed successfully. Other active sessions revoked."}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/avatar", response_model=UserResponse)
async def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user_auth: AuthenticatedUser = Depends(get_current_user),
):
    """
    Safely uploads a user avatar image.
    - Validates file extensions (.png, .jpg, .jpeg, .webp)
    - Validates binary magic bytes to prevent masquerading
    - Enforces a 2 MB file size limit
    - Stores with an isolated UUID filename
    """
    if not user_auth.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Avatar upload is only supported for registered user accounts.",
        )

    user = db.query(User).filter(User.id == user_auth.user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    # 1. Validate file extension
    filename_lower = (file.filename or "").lower()
    allowed_exts = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    ext = Path(filename_lower).suffix
    if ext not in allowed_exts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported image format: {ext}. Allowed formats: PNG, JPEG, WebP.",
        )

    # 2. Read content and enforce 2MB limit
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Avatar image size exceeds 2 MB limit.",
        )

    # 3. Magic bytes validation
    is_valid_magic = False
    if ext == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
        is_valid_magic = True
    elif ext in (".jpg", ".jpeg") and content.startswith(b"\xff\xd8\xff"):
        is_valid_magic = True
    elif ext == ".webp" and content.startswith(b"RIFF") and b"WEBP" in content[:16]:
        is_valid_magic = True

    if not is_valid_magic:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File content does not match image format magic bytes.",
        )

    # 4. Save file safely
    save_filename = f"{user.id}_{Path(file.filename).stem[:8]}_{secrets.token_hex(4)}{ext}"
    save_path = Path(settings.AVATARS_DIR) / save_filename
    with open(save_path, "wb") as f:
        f.write(content)

    user.avatar_url = f"/api/v1/auth/avatar/{save_filename}"
    db.commit()
    db.refresh(user)

    return user_to_response(db, user)


@router.get("/avatar/{filename}")
def serve_avatar(filename: str):
    """Serves user avatar images safely, strictly preventing path traversal."""
    safe_name = os.path.basename(filename)
    if safe_name != filename or ".." in filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename.")

    file_path = Path(settings.AVATARS_DIR) / safe_name
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Avatar not found.")

    ext = file_path.suffix.lower()
    media_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(file_path, media_type=media_type)


# ---------------------------------------------------------------------------
# Administrator User Management Routes
# ---------------------------------------------------------------------------

@router.get("/users", response_model=UserListResponse)
def list_users(
    db: Session = Depends(get_db),
    admin_user: AuthenticatedUser = Depends(require_admin),
):
    """Admin only: lists all registered user accounts."""
    users = auth_service.list_users(db)
    return UserListResponse(
        items=[user_to_response(db, u) for u in users],
        total=len(users),
    )


@router.post("/users/{user_id}/device-binding")
def update_user_device_binding(
    user_id: str,
    req: DeviceBindingRequest,
    db: Session = Depends(get_db),
    admin_user: AuthenticatedUser = Depends(require_admin),
):
    """
    Admin only: grants or revokes vehicle control permission for a user.
    Prevents unauthorized accounts from commanding vehicle hardware.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    binding = auth_service.set_device_binding(
        db,
        user_id=user_id,
        device_id=req.device_id,
        can_control=req.can_control,
        operator_id=admin_user.identity,
    )
    return {
        "code": 200,
        "message": f"Device '{req.device_id}' control permission updated to {req.can_control} for user {user.username}.",
        "binding_id": binding.id,
        "can_control": binding.can_control,
    }


@router.patch("/users/{user_id}/role", response_model=UserResponse)
def update_user_role(
    user_id: str,
    req: UserRoleUpdateRequest,
    db: Session = Depends(get_db),
    admin_user: AuthenticatedUser = Depends(require_admin),
):
    """Admin only: updates user role ('user' or 'admin')."""
    try:
        user = auth_service.set_user_role(db, user_id=user_id, new_role=req.role)
        return user_to_response(db, user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
