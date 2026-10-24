"""Offline SD bookkeeping contract. Paths and raw executor diagnostics are excluded."""
from typing import Annotated, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

Opaque = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Epoch = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
Quality = Literal["128k", "192k", "320k", "lossless", "hires"]
AudioFormat = Literal["mp3", "flac", "m4a", "aac", "ogg", "wav"]
FailureCode = Literal["SOURCE_UNAVAILABLE", "DOWNLOAD_FAILED", "NETWORK_LOST", "STORAGE_FULL", "HASH_MISMATCH", "NOT_OPENABLE", "FILE_MISSING"]


class OfflineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NetworkConfirmRequest(OfflineRequest):
    device_id: Opaque = "car_p3pro"
    network_epoch: Epoch
    unlimited_wifi_confirmed: Literal[True]
    batch_total: int = Field(1, ge=1, le=1000)
    expires_in_minutes: int = Field(60, ge=1, le=10080)


class NetworkConfirmResponse(BaseModel):
    code: int = 200
    device_id: str
    network_epoch: str
    wifi_confirm_token: str
    batch_total: int
    batch_remaining: int
    expires_at: str


class CreateJobRequest(OfflineRequest):
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    device_id: Opaque = "car_p3pro"
    # No arbitrary partial-page limit: the client sends the complete pinned favorite membership.
    favorite_revision: int = Field(..., ge=1)
    song_ids: Optional[List[Annotated[int, Field(strict=True, gt=0)]]] = Field(None, min_length=1)
    quality: Quality = "320k"
    network_epoch: Epoch
    wifi_confirm_token: str = Field(..., min_length=1, max_length=256)


class JobStateRequest(OfflineRequest):
    revision: int = Field(..., ge=1)
    network_epoch: Optional[Epoch] = None
    wifi_confirm_token: Optional[str] = Field(None, min_length=1, max_length=256)


class JobSummary(BaseModel):
    code: int = 200
    job_id: str
    state: str
    quality: str
    device_id: str
    total: int
    done: int
    failed: int
    skipped: int
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
    status: str
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


class ClaimRequest(OfflineRequest):
    device_id: Optional[Opaque] = None
    job_id: str = Field(..., min_length=1, max_length=64)
    limit: int = Field(10, ge=1, le=100)
    network_epoch: Epoch


class ClaimItemResponse(OfflineItemResponse):
    job_id: str
    owner_key: str
    device_id: str
    lease_token: str
    lease_expires_at: str
    work_kind: Literal["download", "verify"]
    attempts: int


class ClaimResponse(BaseModel):
    code: int = 200
    owner_key: str
    items: List[ClaimItemResponse]


class HeartbeatRequest(OfflineRequest):
    device_id: Optional[Opaque] = None
    version: int = Field(..., ge=1)
    lease_token: str = Field(..., pattern=r"^[a-f0-9]{48}$")
    network_epoch: Epoch


class HeartbeatResponse(BaseModel):
    code: int = 200
    item_id: str
    version: int
    status: str
    lease_expires_at: str


class ReceiptRequest(OfflineRequest):
    device_id: Optional[Opaque] = None
    job_id: str = Field(..., min_length=1, max_length=64)
    version: int = Field(..., ge=1)
    lease_token: Optional[str] = Field(None, pattern=r"^[a-f0-9]{48}$")
    network_epoch: Optional[Epoch] = None
    size_bytes: int = Field(..., gt=0)
    sha256: str = Field(..., pattern=r"^[a-fA-F0-9]{64}$")
    openable: bool
    card_volume_id: Opaque
    format: AudioFormat


class RevokeRequest(OfflineRequest):
    device_id: Optional[Opaque] = None
    version: int = Field(..., ge=1)
    reason: Literal["CARD_REMOVED", "CARD_CHANGED", "FILE_MISSING", "NOT_OPENABLE", "HASH_MISMATCH"] = "CARD_REMOVED"


class FailRequest(HeartbeatRequest):
    error_code: FailureCode
