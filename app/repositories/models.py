from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.repositories.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Device(Base):
    __tablename__ = "devices"

    device_id = Column(String(64), primary_key=True, index=True)
    client_type = Column(String(32), default="car")  # car | phone
    token_hash = Column(String(128), nullable=True)
    role = Column(String(32), default="car")
    is_active = Column(Boolean, default=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)


class ParseRequest(Base):
    __tablename__ = "parse_requests"

    request_id = Column(String(64), primary_key=True, index=True)
    idempotency_key = Column(String(128), index=True, nullable=True)
    caller_id = Column(String(64), nullable=True)
    source = Column(String(32), nullable=False)  # coords | text | share_link | voice
    raw_input = Column(Text, nullable=True)
    transcribed_text = Column(Text, nullable=True)
    client_lat = Column(Float, nullable=True)
    client_lon = Column(Float, nullable=True)
    client_time = Column(String(64), nullable=True)
    status = Column(String(32), default="resolving")  # resolving | needs_clarification | resolved | failed
    clarification_question = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    task_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    candidates = relationship("Candidate", back_populates="request", cascade="all, delete-orphan")


class Candidate(Base):
    __tablename__ = "candidates"

    candidate_id = Column(String(64), primary_key=True, index=True)
    request_id = Column(String(64), ForeignKey("parse_requests.request_id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(256), nullable=False)
    address = Column(String(512), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    coord_system = Column(String(16), default="wgs84")
    source = Column(String(64), default="manual")
    place_id = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now)

    request = relationship("ParseRequest", back_populates="candidates")


class Task(Base):
    __tablename__ = "tasks"

    task_id = Column(String(64), primary_key=True, index=True)
    request_id = Column(String(64), nullable=True, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    title = Column(String(256), nullable=False)
    address = Column(String(512), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    coord_system = Column(String(16), default="wgs84")
    status = Column(String(32), default="queued", index=True)  # queued | received | launch_requested | failed | expired | superseded
    created_at = Column(DateTime(timezone=True), default=utc_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    superseded_by = Column(String(64), nullable=True)
    delivery_attempts = Column(Integer, default=0)
    last_delivered_at = Column(DateTime(timezone=True), nullable=True)
    ack_deadline = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)

    events = relationship("TaskEvent", back_populates="task", cascade="all, delete-orphan", order_by="TaskEvent.seq")


class TaskEvent(Base):
    __tablename__ = "task_events"

    event_id = Column(String(64), primary_key=True, index=True)
    task_id = Column(String(64), ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, default=1)
    phase = Column(String(32), nullable=False)  # received | launch_requested | failed
    device_timestamp = Column(DateTime(timezone=True), nullable=True)
    server_timestamp = Column(DateTime(timezone=True), default=utc_now)
    error_code = Column(String(64), nullable=True)
    error_detail = Column(Text, nullable=True)

    task = relationship("Task", back_populates="events")


class MediaTrack(Base):
    __tablename__ = "media_tracks"

    track_id = Column(String(64), primary_key=True, index=True)
    source = Column(String(32), default="local_repo", nullable=False)  # local_repo | netease_cache
    title = Column(String(256), nullable=False, index=True)
    artist = Column(String(256), default="Unknown Artist", nullable=False, index=True)
    album = Column(String(256), nullable=True)
    duration_ms = Column(Integer, default=0, nullable=False)
    file_path = Column(Text, nullable=False)
    file_size_bytes = Column(Integer, nullable=False)
    bitrate_kbps = Column(Integer, nullable=True)
    cover_url = Column(Text, nullable=True)
    hit_count = Column(Integer, default=0)
    last_accessed_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)


class CarDeviceStatus(Base):
    __tablename__ = "car_device_status"

    device_id = Column(String(64), primary_key=True, index=True)
    playback_state = Column(String(32), default="stopped", nullable=False)  # playing | paused | stopped
    current_track_id = Column(String(64), nullable=True)
    position_ms = Column(Integer, default=0, nullable=False)
    duration_ms = Column(Integer, default=0, nullable=False)
    software_volume = Column(Float, default=1.0, nullable=False)
    loop_mode = Column(String(16), default="all", nullable=False)  # all | one | off
    shuffle_enabled = Column(Boolean, default=False, nullable=False)
    last_heartbeat_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class MusicCommand(Base):
    __tablename__ = "music_commands"

    command_id = Column(String(64), primary_key=True, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    action = Column(String(32), nullable=False)
    params_json = Column(Text, nullable=True)
    status = Column(String(32), default="queued", index=True)  # queued | sent | received | applied | failed | expired
    sender_role = Column(String(32), default="phone")
    dispatched_channel = Column(String(32), nullable=True)  # ws | http
    delivery_attempts = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=utc_now, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String(64), nullable=True)
    error_detail = Column(Text, nullable=True)


class FavoriteFolder(Base):
    __tablename__ = "favorite_folders"
    __table_args__ = (
        UniqueConstraint("owner_scope", "name_norm", name="uq_favorite_folders_scope_name_norm"),
    )

    id = Column(String(64), primary_key=True, index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    is_legacy = Column(Boolean, default=False, nullable=False)
    owner_scope = Column(String(64), nullable=False, default="__legacy__", index=True)
    name = Column(String(64), nullable=False, index=True)
    name_norm = Column(String(64), nullable=False, default="", index=True)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)



class Favorite(Base):
    __tablename__ = "favorites"

    id = Column(String(64), primary_key=True, index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    is_legacy = Column(Boolean, default=False, nullable=False)
    folder_id = Column(String(64), ForeignKey("favorite_folders.id", ondelete="SET NULL"), nullable=True, index=True)
    name = Column(String(128), nullable=True, index=True)
    address = Column(String(512), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    coord_system = Column(String(32), default="wgs84", nullable=False)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    folder = relationship("FavoriteFolder", foreign_keys=[folder_id])



class User(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True, index=True)
    username = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    nickname = Column(String(64), nullable=True)
    avatar_url = Column(String(512), nullable=True)
    role = Column(String(32), default="user", nullable=False)  # user | admin
    is_active = Column(Boolean, default=True, nullable=False)
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    device_bindings = relationship("UserDeviceBinding", back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    session_id = Column(String(64), primary_key=True, index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_active_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    is_revoked = Column(Boolean, default=False, nullable=False)
    user_agent = Column(String(256), nullable=True)
    ip_address = Column(String(64), nullable=True)

    user = relationship("User", back_populates="sessions")


class UserDeviceBinding(Base):
    __tablename__ = "user_device_bindings"

    id = Column(String(64), primary_key=True, index=True)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    can_control = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    created_by = Column(String(64), nullable=True)

    user = relationship("User", back_populates="device_bindings")

