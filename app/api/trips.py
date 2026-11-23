import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.auth import get_current_user, require_car_or_admin
from app.repositories.database import get_db
from app.repositories.models import Trip, TripPoint
from app.schemas.auth import AuthenticatedUser
from app.services.device_service import is_same_device, resolve_car_device_id

router = APIRouter(prefix="/api/v1/trips", tags=["Trips"])

# Trips with average speed below 1.0 km/h are treated as stationary/GPS drift and ignored.
MIN_VALID_AVG_SPEED_KMH = 1.0


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
    avg_speed_kmh: float = 0.0
    start_lat: Optional[float] = None
    start_lon: Optional[float] = None
    end_lat: Optional[float] = None
    end_lon: Optional[float] = None
    status: str


class DailyMileageItem(BaseModel):
    date: str
    label: str
    distance_km: float
    trip_count: int


class DailyMileageStatsResponse(BaseModel):
    days: int
    total_distance_km: float
    total_trips: int
    items: List[DailyMileageItem]


class TripDetailOut(TripSummaryOut):
    points: List[TripPointOut]


class TripPointsUploadResponse(BaseModel):
    trip_id: str
    accepted: int
    point_count: int
    distance_km: float


def _trip_avg_speed_kmh(trip: Trip) -> float:
    dist = float(trip.distance_km or 0.0)
    if dist <= 0.0 or trip.started_at is None or trip.ended_at is None:
        return 0.0
    dur_sec = (_parse_utc(_iso_utc(trip.ended_at)) - _parse_utc(_iso_utc(trip.started_at))).total_seconds()
    if dur_sec <= 0.0:
        return 0.0
    return (dist / dur_sec) * 3600.0


def _is_valid_trip(trip: Trip) -> bool:
    return _trip_avg_speed_kmh(trip) >= MIN_VALID_AVG_SPEED_KMH


def _to_summary(trip: Trip) -> TripSummaryOut:
    return TripSummaryOut(
        trip_id=trip.trip_id,
        device_id=trip.device_id,
        started_at=_iso_utc(trip.started_at),
        ended_at=_iso_utc(trip.ended_at),
        point_count=trip.point_count,
        distance_km=round(float(trip.distance_km or 0.0), 3),
        avg_speed_kmh=round(_trip_avg_speed_kmh(trip), 2),
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

    # Always keep the vehicle's last known parking spot synchronized with the latest GPS point
    last_ts, last_pt = parsed_points[-1]
    from app.api.vehicle import update_last_parking_spot

    update_last_parking_spot(db, norm_device, last_pt.latitude, last_pt.longitude, recorded_at=last_ts)

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

    # Prune older finished stationary trips (< 1 km/h average speed) for this device
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    stale_candidates = (
        db.query(Trip)
        .filter(Trip.device_id == norm_device, Trip.trip_id != trip.trip_id, Trip.ended_at < cutoff)
        .all()
    )
    for old_trip in stale_candidates:
        if not _is_valid_trip(old_trip):
            db.query(TripPoint).filter(TripPoint.trip_id == old_trip.trip_id).delete()
            db.delete(old_trip)

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


@router.get("/stats/daily", response_model=DailyMileageStatsResponse)
def get_daily_mileage_stats(
    days: int = Query(default=7, ge=1, le=90),
    tz_offset_minutes: int = Query(default=540, ge=-720, le=840),
    device_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_trip_read_access(user)
    tz = timezone(timedelta(minutes=tz_offset_minutes))
    now_local = datetime.now(tz)
    today_local = now_local.date()
    start_date = today_local - timedelta(days=days - 1)
    start_utc = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz).astimezone(timezone.utc)

    query = db.query(Trip).filter(Trip.started_at >= start_utc)
    if device_id:
        query = query.filter(Trip.device_id == resolve_car_device_id(device_id))
    elif user.role == "car" and user.device_id:
        query = query.filter(Trip.device_id == resolve_car_device_id(user.device_id))

    buckets: Dict[str, Dict[str, float]] = {}
    for offset in range(days):
        d = start_date + timedelta(days=offset)
        buckets[d.isoformat()] = {"distance_km": 0.0, "trip_count": 0}

    total_distance = 0.0
    total_trips = 0
    for t in query.all():
        if not _is_valid_trip(t):
            continue
        local_day = _parse_utc(_iso_utc(t.started_at)).astimezone(tz).date().isoformat()
        if local_day in buckets:
            dist = float(t.distance_km or 0.0)
            buckets[local_day]["distance_km"] += dist
            buckets[local_day]["trip_count"] += 1
            total_distance += dist
            total_trips += 1

    items: List[DailyMileageItem] = []
    for offset in range(days):
        d = start_date + timedelta(days=offset)
        key = d.isoformat()
        b = buckets[key]
        items.append(
            DailyMileageItem(
                date=key,
                label=f"{d.month}/{d.day}",
                distance_km=round(float(b["distance_km"]), 2),
                trip_count=int(b["trip_count"]),
            )
        )

    return DailyMileageStatsResponse(
        days=days,
        total_distance_km=round(total_distance, 2),
        total_trips=total_trips,
        items=items,
    )


@router.get("/footprint")
def get_trips_footprint(
    device_id: Optional[str] = Query(default=None),
    limit_trips: int = Query(default=60, ge=1, le=200),
    max_points_per_trip: int = Query(default=120, ge=20, le=300),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Returns all valid trip polylines (downsampled) and aggregate exploration metrics for the city footprint map."""
    _require_trip_read_access(user)
    query = db.query(Trip)
    if device_id:
        query = query.filter(Trip.device_id == resolve_car_device_id(device_id))
    elif user.role == "car" and user.device_id:
        query = query.filter(Trip.device_id == resolve_car_device_id(user.device_id))

    candidates = query.order_by(Trip.started_at.desc()).limit(limit_trips * 3).all()
    valid = [t for t in candidates if _is_valid_trip(t)][:limit_trips]

    total_distance = 0.0
    total_duration_sec = 0.0
    anchor_lat: Optional[float] = None
    anchor_lon: Optional[float] = None
    max_span_km = 0.0
    routes: List[List[Dict[str, float]]] = []

    for trip in valid:
        total_distance += float(trip.distance_km or 0.0)
        if trip.started_at and trip.ended_at:
            dur = (_parse_utc(_iso_utc(trip.ended_at)) - _parse_utc(_iso_utc(trip.started_at))).total_seconds()
            if dur > 0:
                total_duration_sec += dur
        pts = (
            db.query(TripPoint)
            .filter(TripPoint.trip_id == trip.trip_id)
            .order_by(TripPoint.recorded_at.asc(), TripPoint.id.asc())
            .all()
        )
        if len(pts) < 2:
            continue
        if anchor_lat is None:
            anchor_lat = pts[0].latitude
            anchor_lon = pts[0].longitude
        step = max(1, len(pts) // max_points_per_trip)
        sampled = pts[::step]
        if sampled[-1].id != pts[-1].id:
            sampled.append(pts[-1])
        route_pts: List[Dict[str, float]] = []
        for p in sampled:
            if anchor_lat is not None and anchor_lon is not None:
                d_km = _haversine_km(anchor_lat, anchor_lon, p.latitude, p.longitude)
                if d_km > max_span_km:
                    max_span_km = d_km
            route_pts.append({"latitude": p.latitude, "longitude": p.longitude})
        routes.append(route_pts)

    return {
        "total_trips": len(valid),
        "total_distance_km": round(total_distance, 2),
        "total_duration_minutes": round(total_duration_sec / 60.0, 1),
        "max_span_km": round(max_span_km, 2),
        "route_count": len(routes),
        "routes": routes,
    }


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
    candidates = query.order_by(Trip.started_at.desc()).limit(limit * 4).all()
    valid_trips = [t for t in candidates if _is_valid_trip(t)][:limit]
    return TripListResponse(trips=[_to_summary(t) for t in valid_trips])


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
