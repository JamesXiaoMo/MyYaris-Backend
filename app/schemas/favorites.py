import math
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class FavoriteBase(BaseModel):
    name: Optional[str] = Field(None, max_length=128, description="Optional custom name or label")
    address: str = Field(..., max_length=512, description="Address text")
    latitude: float = Field(..., description="Latitude [-90.0, 90.0]")
    longitude: float = Field(..., description="Longitude [-180.0, 180.0]")
    coord_system: Optional[str] = Field("wgs84", max_length=32, description="Coordinate system")

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Address cannot be empty or whitespace only.")
        return v.strip()

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        stripped = v.strip()
        return stripped if stripped else None

    @field_validator("latitude")
    @classmethod
    def validate_latitude(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("Latitude must be a finite number.")
        if v < -90.0 or v > 90.0:
            raise ValueError("Latitude must be between -90.0 and 90.0 degrees.")
        return v

    @field_validator("longitude")
    @classmethod
    def validate_longitude(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("Longitude must be a finite number.")
        if v < -180.0 or v > 180.0:
            raise ValueError("Longitude must be between -180.0 and 180.0 degrees.")
        return v

    @field_validator("coord_system")
    @classmethod
    def validate_coord_system(cls, v: Optional[str]) -> str:
        if not v or not v.strip():
            return "wgs84"
        norm = v.strip().lower()
        if norm != "wgs84":
            raise ValueError("Only 'wgs84' coordinate system is supported in this version. GCJ02/BD09 without explicit transformation is rejected.")
        return norm


class FavoriteCreateRequest(FavoriteBase):
    pass


class FavoriteUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    address: Optional[str] = Field(None, max_length=512)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    coord_system: Optional[str] = Field(None, max_length=32)

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Address cannot be empty or whitespace only.")
        return v.strip()

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        stripped = v.strip()
        return stripped if stripped else None

    @field_validator("latitude")
    @classmethod
    def validate_latitude(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if not math.isfinite(v):
            raise ValueError("Latitude must be a finite number.")
        if v < -90.0 or v > 90.0:
            raise ValueError("Latitude must be between -90.0 and 90.0 degrees.")
        return v

    @field_validator("longitude")
    @classmethod
    def validate_longitude(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if not math.isfinite(v):
            raise ValueError("Longitude must be a finite number.")
        if v < -180.0 or v > 180.0:
            raise ValueError("Longitude must be between -180.0 and 180.0 degrees.")
        return v

    @field_validator("coord_system")
    @classmethod
    def validate_coord_system(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        norm = v.strip().lower()
        if norm != "wgs84":
            raise ValueError("Only 'wgs84' coordinate system is supported in this version. GCJ02/BD09 without explicit transformation is rejected.")
        return norm


class FavoriteResponse(BaseModel):
    id: str
    name: Optional[str] = None
    address: str
    latitude: float
    longitude: float
    coord_system: str
    created_at: str
    updated_at: str


class FavoriteListResponse(BaseModel):
    items: List[FavoriteResponse]
    total: int
    limit: int
    offset: int
