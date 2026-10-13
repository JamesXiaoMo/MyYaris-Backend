import ipaddress
import logging
import secrets
import urllib.parse
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy.orm import Session

from app.config import settings
from app.repositories.models import (
    User,
    UserDeviceBinding,
    UserSession,
    utc_now,
)
from app.schemas.auth import (
    BootstrapAdminRequest,
    ChangePasswordRequest,
    UpdateProfileRequest,
    UserRegisterRequest,
    UserResponse,
)

logger = logging.getLogger(__name__)

# Argon2id password hasher configuration (OWASP recommended parameters)
_ph = PasswordHasher(
    time_cost=2,
    memory_cost=19456,  # 19 MiB
    parallelism=1,
    hash_len=32,
)


def hash_password(password: str) -> str:
    """Hashes a plaintext password using Argon2id."""
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Verifies a plaintext password against an Argon2id hash."""
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, Exception):
        return False


def validate_avatar_url(url: Optional[str]) -> Optional[str]:
    """
    Strictly validates avatar URL to prevent SSRF vulnerabilities.
    Rejects private IPs, loopbacks, internal hostnames, and non-http(s) schemes.
    """
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if len(url) > 512:
        raise ValueError("Avatar URL must not exceed 512 characters.")

    # Allow relative local paths to avatar endpoint
    if url.startswith("/api/v1/auth/avatar/"):
        return url

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Avatar URL must use http or https scheme.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid avatar URL host.")

    lower_host = hostname.lower()
    # Reject common internal domains / metadata endpoints
    if (
        lower_host in ("localhost", "127.0.0.1", "::1", "metadata.google.internal")
        or lower_host.endswith(".local")
        or lower_host.endswith(".internal")
        or lower_host.endswith(".lan")
    ):
        raise ValueError("Internal or loopback avatar URLs are strictly prohibited (SSRF prevention).")

    is_ip = False
    try:
        ip = ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError:
        pass

    if is_ip and (ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local):
        raise ValueError("Private or loopback IP avatar URLs are strictly prohibited (SSRF prevention).")

    return url


def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import timezone
        return dt.replace(tzinfo=timezone.utc)
    return dt


def has_user_device_control(db: Session, user_id: str, device_id: str = "car_p3pro") -> bool:
    """Checks if a user has active permission to control the target vehicle device."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False
    if user.role == "admin":
        return True

    binding = (
        db.query(UserDeviceBinding)
        .filter(
            UserDeviceBinding.user_id == user_id,
            UserDeviceBinding.device_id == device_id,
            UserDeviceBinding.can_control.is_(True),
        )
        .first()
    )
    return binding is not None


def user_to_response(db: Session, user: User) -> UserResponse:
    """Converts a User ORM model to a safe public UserResponse schema."""
    can_control = (user.role == "admin") or has_user_device_control(db, user.id)
    return UserResponse(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
        avatar_url=user.avatar_url,
        role=user.role,
        is_active=user.is_active,
        has_device_control=can_control,
        created_at=user.created_at.isoformat() if user.created_at else "",
        updated_at=user.updated_at.isoformat() if user.updated_at else "",
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    )


class AuthService:
    def bootstrap_admin(self, db: Session, req: BootstrapAdminRequest) -> User:
        """
        Creates the very first admin user.
        Strictly disallowed once any user already exists in the database.
        """
        existing_users_count = db.query(User).count()
        if existing_users_count > 0:
            raise PermissionError("Bootstrap is closed: Administrator already exists.")

        username_clean = req.username.strip().lower()
        pwd_hash = hash_password(req.password)
        now = utc_now()

        admin_user = User(
            id=f"user_{uuid.uuid4().hex[:12]}",
            username=username_clean,
            password_hash=pwd_hash,
            nickname=req.nickname or "System Administrator",
            role="admin",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(admin_user)
        db.commit()
        db.refresh(admin_user)
        logger.info(f"Initial administrator '{username_clean}' successfully bootstrapped.")
        return admin_user

    def register_user(self, db: Session, req: UserRegisterRequest) -> User:
        """
        Registers a new standard user.
        Always assigned role='user' with zero vehicle control permission by default.
        """
        username_clean = req.username.strip().lower()

        existing = db.query(User).filter(User.username == username_clean).first()
        if existing:
            raise ValueError(f"Username '{username_clean}' is already registered.")

        safe_avatar = validate_avatar_url(req.avatar_url)
        pwd_hash = hash_password(req.password)
        now = utc_now()

        new_user = User(
            id=f"user_{uuid.uuid4().hex[:12]}",
            username=username_clean,
            password_hash=pwd_hash,
            nickname=req.nickname or username_clean,
            avatar_url=safe_avatar,
            role="user",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        logger.info(f"User '{username_clean}' registered successfully with role 'user'.")
        return new_user

    def authenticate_user(
        self,
        db: Session,
        username: str,
        password: str,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Tuple[User, UserSession]:
        """
        Authenticates username and password.
        Returns (user, session) on success.
        Raises ValueError with generic error message on failure to prevent user enumeration.
        """
        username_clean = username.strip().lower()
        user = db.query(User).filter(User.username == username_clean).first()

        now = utc_now()

        # Check account lockout if user exists
        if user and user.locked_until:
            lock_time = _ensure_aware(user.locked_until)
            if lock_time and lock_time > now:
                logger.warning(f"Login attempt on locked account '{username_clean}'")
                raise PermissionError("Account is temporarily locked due to excessive failed attempts. Please try again later.")

        # If user does not exist or account is inactive
        if not user or not user.is_active:
            # Constant-time dummy verify to mitigate timing attacks
            _ph.hash("dummy_password_timing_defense")
            raise ValueError("Invalid username or password.")

        # Verify password with Argon2id
        if not verify_password(user.password_hash, password):
            user.failed_login_attempts += 1
            if user.failed_login_attempts >= settings.MAX_FAILED_LOGIN_ATTEMPTS:
                user.locked_until = now + timedelta(minutes=settings.LOCKOUT_MINUTES)
                logger.warning(
                    f"User '{username_clean}' reached maximum failed login attempts. Locked for {settings.LOCKOUT_MINUTES} minutes."
                )
            db.commit()
            raise ValueError("Invalid username or password.")

        # Authentication success: reset failed attempts
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now

        # Create new server-side session
        session_id = secrets.token_urlsafe(32)
        expires_at = now + timedelta(days=settings.SESSION_EXPIRE_DAYS)

        session = UserSession(
            session_id=session_id,
            user_id=user.id,
            created_at=now,
            expires_at=expires_at,
            last_active_at=now,
            is_revoked=False,
            user_agent=user_agent[:256] if user_agent else None,
            ip_address=ip_address[:64] if ip_address else None,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        logger.info(f"User '{username_clean}' successfully authenticated. Session created.")
        return user, session

    def validate_session(self, db: Session, session_id: str) -> Optional[Tuple[User, UserSession]]:
        """Validates session token and returns active User and Session, or None if invalid/expired/revoked."""
        if not session_id or not session_id.strip():
            return None

        session = (
            db.query(UserSession)
            .filter(
                UserSession.session_id == session_id.strip(),
                UserSession.is_revoked.is_(False),
            )
            .first()
        )
        if not session:
            return None

        now = utc_now()
        # Ensure timezone-aware comparison
        exp = session.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=now.tzinfo)

        if exp < now:
            logger.debug(f"Session {session_id[:8]}... expired.")
            return None

        user = db.query(User).filter(User.id == session.user_id, User.is_active.is_(True)).first()
        if not user:
            return None

        # Update last_active_at periodically
        session.last_active_at = now
        db.commit()
        return user, session

    def revoke_session(self, db: Session, session_id: str) -> bool:
        """Revokes a specific session (logout)."""
        session = db.query(UserSession).filter(UserSession.session_id == session_id).first()
        if session:
            session.is_revoked = True
            db.commit()
            return True
        return False

    def revoke_all_user_sessions(self, db: Session, user_id: str, keep_session_id: Optional[str] = None) -> int:
        """Revokes all sessions of a user (e.g. after password change)."""
        query = db.query(UserSession).filter(UserSession.user_id == user_id, UserSession.is_revoked.is_(False))
        if keep_session_id:
            query = query.filter(UserSession.session_id != keep_session_id)
        count = query.update({"is_revoked": True})
        db.commit()
        return count

    def change_password(
        self,
        db: Session,
        user: User,
        req: ChangePasswordRequest,
        current_session_id: Optional[str] = None,
    ) -> None:
        """Changes user password with old password verification and revokes other active sessions."""
        if not verify_password(user.password_hash, req.old_password):
            raise ValueError("Current password verification failed.")

        user.password_hash = hash_password(req.new_password)
        user.updated_at = utc_now()
        db.commit()

        # Revoke other sessions to enforce re-login
        self.revoke_all_user_sessions(db, user.id, keep_session_id=current_session_id)
        logger.info(f"Password changed for user '{user.username}'. Other sessions revoked.")

    def update_profile(self, db: Session, user: User, req: UpdateProfileRequest) -> User:
        """Updates user profile (nickname, avatar_url)."""
        if req.nickname is not None:
            user.nickname = req.nickname.strip() or user.username
        if req.avatar_url is not None:
            user.avatar_url = validate_avatar_url(req.avatar_url)
        user.updated_at = utc_now()
        db.commit()
        db.refresh(user)
        return user

    def set_user_role(self, db: Session, user_id: str, new_role: str) -> User:
        """Admin only: updates user role."""
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("User not found.")
        user.role = new_role
        user.updated_at = utc_now()
        db.commit()
        db.refresh(user)
        return user

    def set_device_binding(
        self,
        db: Session,
        user_id: str,
        device_id: str,
        can_control: bool,
        operator_id: Optional[str] = None,
    ) -> UserDeviceBinding:
        """Admin only: grants or revokes vehicle control permission for a user."""
        binding = (
            db.query(UserDeviceBinding)
            .filter(
                UserDeviceBinding.user_id == user_id,
                UserDeviceBinding.device_id == device_id,
            )
            .first()
        )
        now = utc_now()
        if binding:
            binding.can_control = can_control
        else:
            binding = UserDeviceBinding(
                id=f"bind_{uuid.uuid4().hex[:12]}",
                user_id=user_id,
                device_id=device_id,
                can_control=can_control,
                created_at=now,
                created_by=operator_id,
            )
            db.add(binding)
        db.commit()
        db.refresh(binding)
        return binding

    def list_users(self, db: Session) -> List[User]:
        return db.query(User).order_by(User.created_at.asc()).all()


auth_service = AuthService()
