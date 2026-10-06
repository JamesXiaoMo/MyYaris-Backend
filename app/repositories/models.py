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
