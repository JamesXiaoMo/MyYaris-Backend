import math
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class FavoriteFolderCreateRequest(BaseModel):
    name: str = Field(..., description="Folder name (1-64 characters after trimming)")

    @field_validator("name", mode="before")
    @classmethod
    def validate_folder_name(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError("Folder name must be a string.")
        stripped = v.strip()
        if not stripped:
            raise ValueError("Folder name cannot be empty or whitespace only.")
        if len(stripped) > 64:
            raise ValueError("Folder name must be at most 64 characters.")
        return stripped


class FavoriteFolderUpdateRequest(FavoriteFolderCreateRequest):
    pass


class FavoriteFolderResponse(BaseModel):
    id: str
    name: str
    favorite_count: int = 0
    created_at: str
    updated_at: str


class FavoriteFolderListResponse(BaseModel):
    items: List[FavoriteFolderResponse]
    total: int
    limit: int
    offset: int
    has_more: bool
    uncategorized_count: int = 0


class FavoriteBase(BaseModel):
    name: Optional[str] = Field(None, max_length=128, description="Optional custom name or label")
    address: Optional[str] = Field(default="", max_length=512, description="Optional address text (defaults to empty string)")
    latitude: float = Field(..., description="Latitude [-90.0, 90.0]")
    longitude: float = Field(..., description="Longitude [-180.0, 180.0]")
    coord_system: Optional[str] = Field("wgs84", max_length=32, description="Coordinate system")
    folder_id: Optional[str] = Field(None, max_length=64, description="Optional folder ID")

    @field_validator("folder_id")
    @classmethod
    def validate_folder_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        stripped = v.strip()
        return stripped if stripped else None

    @field_validator("address", mode="before")
    @classmethod
    def validate_address(cls, v: Optional[str]) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError("Address must be a string or null.")
        stripped = v.strip()
        if len(stripped) > 512:
            raise ValueError("Address must be at most 512 characters.")
        return stripped

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("Name must be a string or null.")
        stripped = v.strip()
        if len(stripped) > 128:
            raise ValueError("Name must be at most 128 characters.")
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
    folder_id: Optional[str] = Field(None, max_length=64)

    @field_validator("folder_id")
    @classmethod
    def validate_folder_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        stripped = v.strip()
        return stripped if stripped else None

    @field_validator("address", mode="before")
    @classmethod
    def validate_address(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError("Address must be a string or null.")
        stripped = v.strip()
        if len(stripped) > 512:
            raise ValueError("Address must be at most 512 characters.")
        return stripped

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("Name must be a string or null.")
        stripped = v.strip()
        if len(stripped) > 128:
            raise ValueError("Name must be at most 128 characters.")
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
    folder_id: Optional[str] = None
    folder_name: Optional[str] = None
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
    has_more: bool = False

