from typing import Optional
from pydantic import BaseModel, Field, field_validator


class Coordinate(BaseModel):
    latitude: float = Field(..., description="Latitude in degrees [-90, 90]")
    longitude: float = Field(..., description="Longitude in degrees [-180, 180]")
    coord_system: str = Field(default="wgs84", description="Coordinate reference system")

    @field_validator("latitude")
    @classmethod
    def validate_latitude(cls, v: float) -> float:
        if not (-90.0 <= v <= 90.0):
            raise ValueError("Latitude must be between -90.0 and 90.0")
        return v

    @field_validator("longitude")
    @classmethod
    def validate_longitude(cls, v: float) -> float:
        if not (-180.0 <= v <= 180.0):
            raise ValueError("Longitude must be between -180.0 and 180.0")
        return v


class ErrorResponse(BaseModel):
    code: str
    message: str
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: str
