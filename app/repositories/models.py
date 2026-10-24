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
    applied_mode = Column(String(32), default="list_loop", nullable=True)  # list_loop | shuffle | intelligence | single_loop
    requested_mode = Column(String(32), default="list_loop", nullable=True)
    queue_id = Column(String(64), nullable=True)
    queue_version = Column(Integer, default=1, nullable=True)
    queue_source = Column(String(32), default="netease_favorite", nullable=True)
    current_index = Column(Integer, default=0, nullable=True)
    consecutive_failures = Column(Integer, default=0, nullable=True)
    error_code = Column(String(64), nullable=True)
    error_detail = Column(Text, nullable=True)
    capabilities_json = Column(Text, nullable=True)
    status_seq = Column(Integer, default=0, nullable=False)
    last_applied_command_id = Column(String(64), nullable=True)
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


class QueueVersionSequence(Base):
    __tablename__ = "queue_version_sequences"

    device_id = Column(String(64), primary_key=True, index=True)
    last_allocated_version = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


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


class OfflineJob(Base):
    """A single bulk offline-download job, idempotent per (owner_key, idempotency_key)."""

    __tablename__ = "offline_jobs"
    __table_args__ = (
        UniqueConstraint("owner_key", "idempotency_key", name="uq_offline_jobs_owner_idem"),
    )

    job_id = Column(String(64), primary_key=True, index=True)
    owner_key = Column(String(64), nullable=False, index=True)  # opaque verified NetEase owner
    account_id = Column(String(64), nullable=True, index=True)  # MyYaris account (User.id); None for legacy token
    device_id = Column(String(64), nullable=False, index=True)  # canonical car device id
    idempotency_key = Column(String(128), nullable=False, index=True)  # client-supplied
    request_fingerprint = Column(String(64), nullable=False)  # hash of request body
    state = Column(String(32), default="running", nullable=False, index=True)  # running|paused|done|cancelled
    quality = Column(String(16), default="320k", nullable=False)
    total = Column(Integer, default=0, nullable=False)
    done = Column(Integer, default=0, nullable=False)
    failed = Column(Integer, default=0, nullable=False)
    revision = Column(Integer, default=1, nullable=False)  # optimistic-consistency counter (not an immutable snapshot)
    network_epoch = Column(String(128), nullable=True)  # opaque network identifier reported by the car
    wifi_confirm_token_hash = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class OfflineItem(Base):
    """Per-song offline status. Stores no absolute path / SAF URI / credential."""

    __tablename__ = "offline_items"
    __table_args__ = (
        UniqueConstraint("job_id", "song_id", "quality", name="uq_offline_items_job_song_quality"),
    )

    item_id = Column(String(64), primary_key=True, index=True)
    job_id = Column(String(64), nullable=False, index=True)
    owner_key = Column(String(64), nullable=False, index=True)
    account_id = Column(String(64), nullable=True, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    song_id = Column(Integer, nullable=False, index=True)
    quality = Column(String(16), default="320k", nullable=False)
    format = Column(String(16), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=True)
    card_volume_id = Column(String(64), nullable=True)  # opaque volume id, never a path
    status = Column(String(32), default="pending", nullable=False, index=True)  # pending|claimed|saved|failed|revoked
    version = Column(Integer, default=1, nullable=False)  # per-item optimistic concurrency
    error_code = Column(String(64), nullable=True)
    error_detail = Column(Text, nullable=True)
    claim_deadline = Column(DateTime(timezone=True), nullable=True)
    last_receipt_at = Column(DateTime(timezone=True), nullable=True)
    last_verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class NetworkAuthorization(Base):
    """Time-limited, per-batch WiFi authorization bound to (device_id, network_epoch)."""

    __tablename__ = "network_authorizations"
    __table_args__ = (
        UniqueConstraint("device_id", "network_epoch", name="uq_network_auth_device_epoch"),
    )

    id = Column(String(64), primary_key=True, index=True)
    owner_key = Column(String(64), nullable=False, index=True)
    account_id = Column(String(64), nullable=True, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    network_epoch = Column(String(128), nullable=False)  # opaque, reported by the car; server never infers unlimited from SSID
    token_hash = Column(String(128), nullable=False)
    batch_total = Column(Integer, default=1, nullable=False)
    batch_remaining = Column(Integer, default=1, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

