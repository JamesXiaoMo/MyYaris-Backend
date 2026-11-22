import math
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.auth import get_current_user, require_car_or_admin
from app.repositories.database import get_db
from app.repositories.models import Trip, TripPoint
from app.schemas.auth import AuthenticatedUser
from app.services.device_service import is_same_device, resolve_car_device_id

router = APIRouter(prefix="/api/v1/trips", tags=["Trips"])


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _parse_utc(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class TripPointIn(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    speed_kmh: Optional[float] = Field(default=None, ge=0.0, le=400.0)
    accuracy_m: Optional[float] = Field(default=None, ge=0.0, le=5000.0)
    recorded_at: Optional[str] = None

    @field_validator("latitude", "longitude")
    @classmethod
    def check_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("Coordinate must be finite")
        return v


class TripPointsBatchRequest(BaseModel):
    device_id: Optional[str] = None
    trip_id: Optional[str] = Field(default=None, max_length=64)
    points: List[TripPointIn] = Field(..., min_length=1, max_length=200)


class TripPointOut(BaseModel):
    latitude: float
    longitude: float
    speed_kmh: Optional[float] = None
    accuracy_m: Optional[float] = None
    recorded_at: str


class TripSummaryOut(BaseModel):
    trip_id: str
    device_id: str
    started_at: str
    ended_at: str
    point_count: int
    distance_km: float
    start_lat: Optional[float] = None
    start_lon: Optional[float] = None
    end_lat: Optional[float] = None
    end_lon: Optional[float] = None
    status: str


class TripDetailOut(TripSummaryOut):
    points: List[TripPointOut]


class TripPointsUploadResponse(BaseModel):
    trip_id: str
    accepted: int
    point_count: int
    distance_km: float


def _to_summary(trip: Trip) -> TripSummaryOut:
    return TripSummaryOut(
        trip_id=trip.trip_id,
        device_id=trip.device_id,
        started_at=_iso_utc(trip.started_at),
        ended_at=_iso_utc(trip.ended_at),
        point_count=trip.point_count,
        distance_km=round(float(trip.distance_km or 0.0), 3),
        start_lat=trip.start_lat,
        start_lon=trip.start_lon,
        end_lat=trip.end_lat,
        end_lon=trip.end_lon,
        status=trip.status,
    )


def _require_trip_read_access(user: AuthenticatedUser) -> None:
    if user.role == "user" and not user.has_device_control:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Device access required to view trips",
        )


@router.post("/points", response_model=TripPointsUploadResponse)
def upload_trip_points(
    payload: TripPointsBatchRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
):
    norm_device = resolve_car_device_id(payload.device_id, user.device_id)
    if user.role == "car" and user.device_id and not is_same_device(user.device_id, norm_device):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Car token cannot report trips for another device",
        )

    trip_id = (payload.trip_id or "").strip() or f"trip_{uuid.uuid4().hex[:16]}"
    trip = db.query(Trip).filter(Trip.trip_id == trip_id).first()

    parsed_points = []
    for p in payload.points:
        if p.latitude == 0.0 and p.longitude == 0.0:
            continue
        try:
            ts = _parse_utc(p.recorded_at)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid recorded_at: {exc}") from exc
        parsed_points.append((ts, p))

    if not parsed_points:
        raise HTTPException(status_code=422, detail="No valid GPS coordinates in batch")

    parsed_points.sort(key=lambda item: item[0])

    if trip is None:
        first_ts, first_pt = parsed_points[0]
        trip = Trip(
            trip_id=trip_id,
            device_id=norm_device,
            started_at=first_ts,
            ended_at=first_ts,
            point_count=0,
            distance_km=0.0,
            start_lat=first_pt.latitude,
            start_lon=first_pt.longitude,
            end_lat=first_pt.latitude,
            end_lon=first_pt.longitude,
            status="recording",
        )
        db.add(trip)
        db.flush()
    elif not is_same_device(trip.device_id, norm_device):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Trip belongs to another device",
        )

    prev_lat = trip.end_lat if trip.point_count > 0 else None
    prev_lon = trip.end_lon if trip.point_count > 0 else None
    added_distance = 0.0

    for ts, p in parsed_points:
        if prev_lat is not None and prev_lon is not None:
            seg = _haversine_km(prev_lat, prev_lon, p.latitude, p.longitude)
            # Ignore stationary GPS jitter (<5m) and impossible teleport jumps (>50km per 10s sample)
            if 0.005 <= seg <= 50.0:
                added_distance += seg
        prev_lat = p.latitude
        prev_lon = p.longitude

        db.add(
            TripPoint(
                trip_id=trip.trip_id,
                device_id=norm_device,
                latitude=p.latitude,
                longitude=p.longitude,
                speed_kmh=p.speed_kmh,
                accuracy_m=p.accuracy_m,
                recorded_at=ts,
            )
        )

        if trip.start_lat is None:
            trip.start_lat = p.latitude
            trip.start_lon = p.longitude
            trip.started_at = ts
        trip.end_lat = p.latitude
        trip.end_lon = p.longitude
        if trip.ended_at is None or ts > _parse_utc(_iso_utc(trip.ended_at)):
            trip.ended_at = ts

    trip.point_count = (trip.point_count or 0) + len(parsed_points)
    trip.distance_km = float(trip.distance_km or 0.0) + added_distance

    # Keep the vehicle's last known parking spot synchronized with the latest GPS point
    last_ts, last_pt = parsed_points[-1]
    from app.api.vehicle import update_last_parking_spot

    update_last_parking_spot(db, norm_device, last_pt.latitude, last_pt.longitude, recorded_at=last_ts)
    db.commit()
    db.refresh(trip)

    return TripPointsUploadResponse(
        trip_id=trip.trip_id,
        accepted=len(parsed_points),
        point_count=trip.point_count,
        distance_km=round(float(trip.distance_km or 0.0), 3),
    )


class TripListResponse(BaseModel):
    trips: List[TripSummaryOut]


@router.get("", response_model=TripListResponse)
def list_trips(
    device_id: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_trip_read_access(user)
    query = db.query(Trip)
    if device_id:
        norm = resolve_car_device_id(device_id)
        query = query.filter(Trip.device_id == norm)
    elif user.role == "car" and user.device_id:
        query = query.filter(Trip.device_id == resolve_car_device_id(user.device_id))
    trips = query.order_by(Trip.started_at.desc()).limit(limit).all()
    return TripListResponse(trips=[_to_summary(t) for t in trips])


@router.get("/{trip_id}", response_model=TripDetailOut)
def get_trip_detail(
    trip_id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_trip_read_access(user)
    trip = db.query(Trip).filter(Trip.trip_id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    points = (
        db.query(TripPoint)
        .filter(TripPoint.trip_id == trip_id)
        .order_by(TripPoint.recorded_at.asc(), TripPoint.id.asc())
        .all()
    )
    summary = _to_summary(trip)
    return TripDetailOut(
        **summary.model_dump(),
        points=[
            TripPointOut(
                latitude=pt.latitude,
                longitude=pt.longitude,
                speed_kmh=pt.speed_kmh,
                accuracy_m=pt.accuracy_m,
                recorded_at=_iso_utc(pt.recorded_at),
            )
            for pt in points
        ],
    )


@router.delete("/{trip_id}")
def delete_trip(
    trip_id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_trip_read_access(user)
    trip = db.query(Trip).filter(Trip.trip_id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    db.query(TripPoint).filter(TripPoint.trip_id == trip_id).delete()
    db.delete(trip)
    db.commit()
    return {"deleted": True, "trip_id": trip_id}
