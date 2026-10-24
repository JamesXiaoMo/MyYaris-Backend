"""Schemas for the offline SD download feature (V3.0).

No absolute path, SAF URI, or credential is ever represented in these models.
"""
from typing import List, Optional
from pydantic import BaseModel, Field


class NetworkConfirmRequest(BaseModel):
    device_id: str = Field("car_p3pro", min_length=1, max_length=64)
    network_epoch: str = Field(..., min_length=1, max_length=128, description="Opaque network identity reported by the car")
    batch_total: int = Field(1, ge=1, le=1000)
    expires_in_minutes: int = Field(60, ge=1, le=7 * 24 * 60)


class NetworkConfirmResponse(BaseModel):
    code: int = 200
    device_id: str
    network_epoch: str
    wifi_confirm_token: str = Field(..., description="Plaintext token returned once; only its hash is stored")
    batch_total: int
    batch_remaining: int
    expires_at: str


class CreateJobRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    device_id: str = Field("car_p3pro", min_length=1, max_length=64)
    song_ids: List[int] = Field(..., min_length=1)
    quality: str = Field("320k", min_length=1, max_length=16)
    network_epoch: str = Field(..., min_length=1, max_length=128)
    wifi_confirm_token: str = Field(..., min_length=1, max_length=256)


class JobSummary(BaseModel):
    code: int = 200
    job_id: str
    state: str
    quality: str
    device_id: str
    total: int
    done: int
    failed: int
    revision: int
    network_epoch: Optional[str] = None
    created_at: str
    updated_at: str


class JobListResponse(BaseModel):
    code: int = 200
    jobs: List[JobSummary]
    total: int


class OfflineItemResponse(BaseModel):
    item_id: str
    song_id: int
    quality: str
    status: str  # pending | claimed | saved | failed | revoked
    version: int
    format: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    card_volume_id: Optional[str] = None
    error_code: Optional[str] = None
    stale: bool = False
    last_verified_at: Optional[str] = None


class ItemListResponse(BaseModel):
    code: int = 200
    job_id: str
    revision: int
    offset: int
    limit: int
    total: int
    has_more: bool
    items: List[OfflineItemResponse]


class ClaimRequest(BaseModel):
    device_id: Optional[str] = None
    job_id: Optional[str] = None
    limit: int = Field(10, ge=1, le=100)
    network_epoch: Optional[str] = Field(None, description="Car's current network identity; mismatched jobs are skipped")


class ClaimResponse(BaseModel):
    code: int = 200
    items: List[OfflineItemResponse]


class HeartbeatRequest(BaseModel):
    device_id: Optional[str] = None
    version: int = Field(..., ge=1)


class HeartbeatResponse(BaseModel):
    code: int = 200
    item_id: str
    version: int
    status: str


class ReceiptRequest(BaseModel):
    device_id: Optional[str] = None
    job_id: str = Field(..., min_length=1, max_length=64)
    version: int = Field(..., ge=1)
    size_bytes: int = Field(..., ge=0)
    sha256: str = Field(..., min_length=16, max_length=64)
    openable: bool = Field(..., description="Car verified the file is openable")
    card_volume_id: str = Field(..., min_length=1, max_length=64, description="Opaque volume id, never a path")
    format: Optional[str] = Field(None, max_length=16)


class RevokeRequest(BaseModel):
    device_id: Optional[str] = None
    version: int = Field(..., ge=1)
    reason: str = Field("CARD_REMOVED", max_length=64)
