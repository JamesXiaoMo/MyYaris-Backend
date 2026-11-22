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
    """A complete favorite-membership job; stored idempotency keys include account scope."""

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
    skipped = Column(Integer, default=0, nullable=False)  # saved items requiring no claim in this job
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
    version = Column(Integer, default=1, nullable=False)  # per-item optimistic concurrency (CAS)
    attempts = Column(Integer, default=0, nullable=False)  # claim attempts counter (retry cap)
    lease_token = Column(String(64), nullable=True)
    work_kind = Column(String(16), default="download", nullable=False)
    device_generation = Column(Integer, default=1, nullable=False)
    inventory_version = Column(Integer, default=0, nullable=False)
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


class OfflineTrack(Base):
    """Durable account/Owner/device/song/quality inventory, including revocation tombstones."""

    __tablename__ = "offline_tracks"
    __table_args__ = (
        UniqueConstraint("owner_key", "account_id", "device_id", "song_id", "quality", name="uq_offline_tracks_scope"),
    )

    id = Column(String(64), primary_key=True, index=True)
    owner_key = Column(String(64), nullable=False, index=True)
    account_id = Column(String(64), nullable=False, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    song_id = Column(Integer, nullable=False, index=True)
    quality = Column(String(16), default="320k", nullable=False)
    status = Column(String(16), default="revoked", nullable=False)
    size_bytes = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=True)
    card_volume_id = Column(String(64), nullable=True)  # opaque volume id, never a path
    format = Column(String(16), nullable=True)
    version = Column(Integer, default=1, nullable=False)
    last_verified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class OfflineDevice(Base):
    """Physical card generation fences old work, even across account/Owner switches."""

    __tablename__ = "offline_devices"
    device_id = Column(String(64), primary_key=True)
    generation = Column(Integer, default=1, nullable=False)
    card_volume_id = Column(String(64), nullable=True)


class Trip(Base):
    """One driving trip session recorded by the car unit."""

    __tablename__ = "trips"

    trip_id = Column(String(64), primary_key=True, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    ended_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    point_count = Column(Integer, default=0, nullable=False)
    distance_km = Column(Float, default=0.0, nullable=False)
    start_lat = Column(Float, nullable=True)
    start_lon = Column(Float, nullable=True)
    end_lat = Column(Float, nullable=True)
    end_lon = Column(Float, nullable=True)
    status = Column(String(16), default="recording", nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class TripPoint(Base):
    """Individual GPS sample recorded every ~10 seconds during a trip."""

    __tablename__ = "trip_points"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trip_id = Column(
        String(64),
        ForeignKey("trips.trip_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_id = Column(String(64), nullable=False, index=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    speed_kmh = Column(Float, nullable=True)
    accuracy_m = Column(Float, nullable=True)
    recorded_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)


class VehicleState(Base):
    """Latest persisted OBD-II telemetry snapshot & last known parking location per vehicle."""

    __tablename__ = "vehicle_state"

    device_id = Column(String(64), primary_key=True, index=True)

    # --- Fuel & Range (Primary Widget Metrics) ---
    fuel_level_liters = Column(Float, nullable=True)
    fuel_level_percent = Column(Float, nullable=True)
    estimated_range_km = Column(Float, nullable=True)
    total_odometer_km = Column(Float, nullable=True)
    trip_distance_km = Column(Float, nullable=True)
    fuel_rate_l_h = Column(Float, nullable=True)
    total_fuel_consumed_liters = Column(Float, nullable=True)

    # --- Hybrid HV Battery & Electric Drive ---
    battery_soc = Column(Float, nullable=True)
    battery_voltage = Column(Float, nullable=True)
    battery_current = Column(Float, nullable=True)
    battery_power_kw = Column(Float, nullable=True)
    battery_remaining_life = Column(Float, nullable=True)
    max_cell_voltage = Column(Float, nullable=True)
    min_cell_voltage = Column(Float, nullable=True)
    avg_cell_voltage = Column(Float, nullable=True)
    cell_delta_voltage = Column(Float, nullable=True)
    battery_temperatures_json = Column(Text, nullable=True)
    voltage_before_boost = Column(Float, nullable=True)
    voltage_after_boost = Column(Float, nullable=True)
    mg1_inverter_temp = Column(Float, nullable=True)
    mg2_inverter_temp = Column(Float, nullable=True)
    transaxle_oil_temp = Column(Float, nullable=True)
    mg1_rpm = Column(Float, nullable=True)
    mg2_rpm = Column(Float, nullable=True)
    mg2_torque = Column(Float, nullable=True)
    regen_braking_torque = Column(Float, nullable=True)

    # --- Engine & Powertrain ---
    speed_kmh = Column(Float, nullable=True)
    engine_rpm = Column(Float, nullable=True)
    coolant_temp = Column(Float, nullable=True)
    engine_oil_temp = Column(Float, nullable=True)
    engine_oil_pressure = Column(Float, nullable=True)
    electric_water_pump_rpm = Column(Float, nullable=True)
    electric_water_pump_target_rpm = Column(Float, nullable=True)
    intake_air_temp = Column(Float, nullable=True)
    ambient_air_temp = Column(Float, nullable=True)
    catalyst_temp = Column(Float, nullable=True)
    catalyst_temp2 = Column(Float, nullable=True)
    throttle_position = Column(Float, nullable=True)
    relative_throttle_position = Column(Float, nullable=True)
    commanded_throttle_actuator = Column(Float, nullable=True)
    pedal_position_d = Column(Float, nullable=True)
    pedal_position_e = Column(Float, nullable=True)
    engine_load = Column(Float, nullable=True)
    maf_g_s = Column(Float, nullable=True)
    baro_kpa = Column(Float, nullable=True)
    equivalence_ratio = Column(Float, nullable=True)
    short_term_fuel_trim = Column(Float, nullable=True)
    long_term_fuel_trim = Column(Float, nullable=True)
    run_time_seconds = Column(Integer, nullable=True)

    # --- 12V Aux Battery & Electrical ---
    aux_12v_voltage = Column(Float, nullable=True)
    aux_12v_current = Column(Float, nullable=True)
    aux_12v_temp = Column(Float, nullable=True)
    aux_12v_soc = Column(Float, nullable=True)
    key_cycles = Column(Integer, nullable=True)

    # --- Extensible Extra JSON ---
    extra_json = Column(Text, nullable=True)

    # --- Last Known Parking Location ---
    parked_lat = Column(Float, nullable=True)
    parked_lon = Column(Float, nullable=True)
    parked_address = Column(String(256), nullable=True)
    parked_at = Column(DateTime(timezone=True), nullable=True)

    # --- Timestamps ---
    obd_updated_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ObdTelemetryLog(Base):
    """Time-series OBD telemetry history snapshots for trend analysis."""

    __tablename__ = "obd_telemetry_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String(64), nullable=False, index=True)
    recorded_at = Column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    fuel_level_liters = Column(Float, nullable=True)
    fuel_level_percent = Column(Float, nullable=True)
    estimated_range_km = Column(Float, nullable=True)
    total_odometer_km = Column(Float, nullable=True)
    battery_soc = Column(Float, nullable=True)
    battery_voltage = Column(Float, nullable=True)
    battery_power_kw = Column(Float, nullable=True)
    aux_12v_voltage = Column(Float, nullable=True)
    speed_kmh = Column(Float, nullable=True)
    engine_rpm = Column(Float, nullable=True)
    coolant_temp = Column(Float, nullable=True)
    ambient_air_temp = Column(Float, nullable=True)
    payload_json = Column(Text, nullable=True)
