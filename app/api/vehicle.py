from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import get_current_user, require_car_or_admin
from app.repositories.database import get_db
from app.repositories.models import ObdTelemetryLog, Trip, VehicleState, utc_now
from app.schemas.auth import AuthenticatedUser
from app.services.control_service import get_car_status_by_id, ws_manager
from app.services.device_service import is_same_device, resolve_car_device_id

router = APIRouter(prefix="/api/v1/vehicle", tags=["Vehicle OBD & Parking State"])

# Toyota Yaris Hybrid (MXPH10/11) fuel tank capacity & conservative WLTC baseline
YARIS_TANK_LITERS = 36.0
YARIS_DEFAULT_KM_PER_LITER = 22.5

OBD_SCALAR_FIELDS = (
    "fuel_level_liters",
    "fuel_level_percent",
    "estimated_range_km",
    "total_odometer_km",
    "trip_distance_km",
    "fuel_rate_l_h",
    "total_fuel_consumed_liters",
    "battery_soc",
    "battery_voltage",
    "battery_current",
    "battery_power_kw",
    "battery_remaining_life",
    "max_cell_voltage",
    "min_cell_voltage",
    "avg_cell_voltage",
    "cell_delta_voltage",
    "voltage_before_boost",
    "voltage_after_boost",
    "mg1_inverter_temp",
    "mg2_inverter_temp",
    "transaxle_oil_temp",
    "mg1_rpm",
    "mg2_rpm",
    "mg2_torque",
    "regen_braking_torque",
    "speed_kmh",
    "engine_rpm",
    "coolant_temp",
    "engine_oil_temp",
    "engine_oil_pressure",
    "electric_water_pump_rpm",
    "electric_water_pump_target_rpm",
    "intake_air_temp",
    "ambient_air_temp",
    "catalyst_temp",
    "catalyst_temp2",
    "throttle_position",
    "relative_throttle_position",
    "commanded_throttle_actuator",
    "pedal_position_d",
    "pedal_position_e",
    "engine_load",
    "maf_g_s",
    "baro_kpa",
    "equivalence_ratio",
    "short_term_fuel_trim",
    "long_term_fuel_trim",
    "run_time_seconds",
    "aux_12v_voltage",
    "aux_12v_current",
    "aux_12v_temp",
    "aux_12v_soc",
    "key_cycles",
)


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Optional[str]) -> datetime:
    if not value:
        return utc_now()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return utc_now()


def _get_or_create_vehicle_state(db: Session, device_id: str) -> VehicleState:
    row = db.query(VehicleState).filter(VehicleState.device_id == device_id).first()
    if row is None:
        row = VehicleState(device_id=device_id, updated_at=utc_now())
        db.add(row)
        db.flush()
    return row


def update_last_parking_spot(
    db: Session,
    device_id: str,
    latitude: float,
    longitude: float,
    recorded_at: Optional[datetime] = None,
    address: Optional[str] = None,
) -> VehicleState:
    """Updates the persisted last known parking coordinate for a vehicle."""
    norm_device = resolve_car_device_id(device_id)
    row = _get_or_create_vehicle_state(db, norm_device)
    ts = recorded_at or utc_now()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    should_update = row.parked_at is None
    if not should_update and row.parked_at is not None:
        prev_ts = row.parked_at
        if prev_ts.tzinfo is None:
            prev_ts = prev_ts.replace(tzinfo=timezone.utc)
        should_update = ts >= prev_ts

    if should_update:
        row.parked_lat = float(latitude)
        row.parked_lon = float(longitude)
        row.parked_at = ts
        if address is not None:
            row.parked_address = address[:256]
        row.updated_at = utc_now()
    return row


class ObdTelemetryUploadRequest(BaseModel):
    device_id: Optional[str] = None
    recorded_at: Optional[str] = None

    # Fuel & Range
    fuel_level_liters: Optional[float] = None
    fuel_level_percent: Optional[float] = None
    estimated_range_km: Optional[float] = None
    total_odometer_km: Optional[float] = None
    trip_distance_km: Optional[float] = None
    fuel_rate_l_h: Optional[float] = None
    total_fuel_consumed_liters: Optional[float] = None

    # Hybrid HV Battery & Electric Drive
    battery_soc: Optional[float] = None
    battery_voltage: Optional[float] = None
    battery_current: Optional[float] = None
    battery_power_kw: Optional[float] = None
    battery_remaining_life: Optional[float] = None
    max_cell_voltage: Optional[float] = None
    min_cell_voltage: Optional[float] = None
    avg_cell_voltage: Optional[float] = None
    cell_delta_voltage: Optional[float] = None
    battery_temperatures: Optional[List[float]] = None
    voltage_before_boost: Optional[float] = None
    voltage_after_boost: Optional[float] = None
    mg1_inverter_temp: Optional[float] = None
    mg2_inverter_temp: Optional[float] = None
    transaxle_oil_temp: Optional[float] = None
    mg1_rpm: Optional[float] = None
    mg2_rpm: Optional[float] = None
    mg2_torque: Optional[float] = None
    regen_braking_torque: Optional[float] = None

    # Engine & Powertrain
    speed_kmh: Optional[float] = None
    engine_rpm: Optional[float] = None
    coolant_temp: Optional[float] = None
    engine_oil_temp: Optional[float] = None
    engine_oil_pressure: Optional[float] = None
    electric_water_pump_rpm: Optional[float] = None
    electric_water_pump_target_rpm: Optional[float] = None
    intake_air_temp: Optional[float] = None
    ambient_air_temp: Optional[float] = None
    catalyst_temp: Optional[float] = None
    catalyst_temp2: Optional[float] = None
    throttle_position: Optional[float] = None
    relative_throttle_position: Optional[float] = None
    commanded_throttle_actuator: Optional[float] = None
    pedal_position_d: Optional[float] = None
    pedal_position_e: Optional[float] = None
    engine_load: Optional[float] = None
    maf_g_s: Optional[float] = None
    baro_kpa: Optional[float] = None
    equivalence_ratio: Optional[float] = None
    short_term_fuel_trim: Optional[float] = None
    long_term_fuel_trim: Optional[float] = None
    run_time_seconds: Optional[int] = None

    # 12V Aux Battery
    aux_12v_voltage: Optional[float] = None
    aux_12v_current: Optional[float] = None
    aux_12v_temp: Optional[float] = None
    aux_12v_soc: Optional[float] = None
    key_cycles: Optional[int] = None

    # Optional parking coordinate override & extra telemetry
    parked_lat: Optional[float] = Field(None, ge=-90, le=90)
    parked_lon: Optional[float] = Field(None, ge=-180, le=180)
    parked_address: Optional[str] = Field(None, max_length=256)
    extra: Optional[Dict[str, Any]] = None


def _require_vehicle_read_access(user: AuthenticatedUser) -> None:
    if user.role == "user" and not user.has_device_control:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Device access required to view vehicle telemetry",
        )


def _serialize_vehicle_state(row: VehicleState, online: bool) -> Dict[str, Any]:
    temps: List[float] = []
    if row.battery_temperatures_json:
        try:
            parsed = json.loads(row.battery_temperatures_json)
            if isinstance(parsed, list):
                temps = [float(x) for x in parsed]
        except Exception:
            temps = []

    extra: Dict[str, Any] = {}
    if row.extra_json:
        try:
            parsed_extra = json.loads(row.extra_json)
            if isinstance(parsed_extra, dict):
                extra = parsed_extra
        except Exception:
            extra = {}

    data: Dict[str, Any] = {
        "device_id": row.device_id,
        "online": online,
        "tank_capacity_liters": YARIS_TANK_LITERS,
    }
    for field in OBD_SCALAR_FIELDS:
        data[field] = getattr(row, field)
    data["battery_temperatures"] = temps
    data["extra"] = extra
    data["parking"] = {
        "latitude": row.parked_lat,
        "longitude": row.parked_lon,
        "address": row.parked_address,
        "parked_at": _iso_utc(row.parked_at),
    }
    data["obd_updated_at"] = _iso_utc(row.obd_updated_at)
    data["updated_at"] = _iso_utc(row.updated_at)
    return data


@router.post("/telemetry")
def upload_vehicle_telemetry(
    payload: ObdTelemetryUploadRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_car_or_admin),
) -> Dict[str, Any]:
    norm_device = resolve_car_device_id(payload.device_id, user.device_id)
    if user.role == "car" and user.device_id and not is_same_device(user.device_id, norm_device):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Car token cannot upload telemetry for another device",
        )

    ts = _parse_utc(payload.recorded_at)
    row = _get_or_create_vehicle_state(db, norm_device)

    updated_fields: Dict[str, Any] = {}
    for field in OBD_SCALAR_FIELDS:
        val = getattr(payload, field)
        if val is not None:
            setattr(row, field, val)
            updated_fields[field] = val

    if payload.battery_temperatures:
        row.battery_temperatures_json = json.dumps(payload.battery_temperatures)
        updated_fields["battery_temperatures"] = payload.battery_temperatures

    # Derive fuel_level_percent <-> fuel_level_liters and estimated_range_km automatically
    if row.fuel_level_liters is not None and payload.fuel_level_percent is None:
        row.fuel_level_percent = round(
            max(0.0, min(100.0, (row.fuel_level_liters / YARIS_TANK_LITERS) * 100.0)),
            1,
        )
    elif row.fuel_level_percent is not None and payload.fuel_level_liters is None:
        row.fuel_level_liters = round(
            max(0.0, min(YARIS_TANK_LITERS, (row.fuel_level_percent / 100.0) * YARIS_TANK_LITERS)),
            2,
        )

    if payload.estimated_range_km is None and row.fuel_level_liters is not None:
        row.estimated_range_km = round(max(0.0, row.fuel_level_liters * YARIS_DEFAULT_KM_PER_LITER), 1)

    if payload.extra is not None:
        existing_extra: Dict[str, Any] = {}
        if row.extra_json:
            try:
                parsed = json.loads(row.extra_json)
                if isinstance(parsed, dict):
                    existing_extra = parsed
            except Exception:
                pass
        existing_extra.update(payload.extra)
        row.extra_json = json.dumps(existing_extra, ensure_ascii=False)

    if (
        payload.parked_lat is not None
        and payload.parked_lon is not None
        and not (payload.parked_lat == 0.0 and payload.parked_lon == 0.0)
    ):
        update_last_parking_spot(
            db,
            norm_device,
            payload.parked_lat,
            payload.parked_lon,
            recorded_at=ts,
            address=payload.parked_address,
        )

    row.obd_updated_at = ts
    row.updated_at = utc_now()

    log_entry = ObdTelemetryLog(
        device_id=norm_device,
        recorded_at=ts,
        fuel_level_liters=row.fuel_level_liters,
        fuel_level_percent=row.fuel_level_percent,
        estimated_range_km=row.estimated_range_km,
        total_odometer_km=row.total_odometer_km,
        battery_soc=row.battery_soc,
        battery_voltage=row.battery_voltage,
        battery_power_kw=row.battery_power_kw,
        aux_12v_voltage=row.aux_12v_voltage,
        speed_kmh=row.speed_kmh,
        engine_rpm=row.engine_rpm,
        coolant_temp=row.coolant_temp,
        ambient_air_temp=row.ambient_air_temp,
        payload_json=json.dumps(updated_fields, ensure_ascii=False),
    )
    db.add(log_entry)
    db.commit()
    db.refresh(row)

    return {
        "ok": True,
        "device_id": norm_device,
        "log_id": log_entry.id,
        "estimated_range_km": row.estimated_range_km,
        "obd_updated_at": _iso_utc(row.obd_updated_at),
    }


@router.get("/state")
def get_vehicle_state(
    device_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    _require_vehicle_read_access(user)
    norm_device = resolve_car_device_id(device_id, user.device_id)
    row = _get_or_create_vehicle_state(db, norm_device)

    # Backfill parking location from latest recorded Trip if not yet populated
    if row.parked_lat is None or row.parked_lon is None:
        latest_trip = (
            db.query(Trip)
            .filter(Trip.device_id == norm_device, Trip.end_lat.isnot(None), Trip.end_lon.isnot(None))
            .order_by(Trip.ended_at.desc())
            .first()
        )
        if latest_trip is not None:
            row.parked_lat = latest_trip.end_lat
            row.parked_lon = latest_trip.end_lon
            row.parked_at = latest_trip.ended_at
            db.commit()
            db.refresh(row)

    car_status = get_car_status_by_id(db, norm_device)
    online = bool(ws_manager.has_car_client(norm_device))
    if not online and car_status and car_status.last_heartbeat_at:
        hb = car_status.last_heartbeat_at
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        online = (datetime.now(timezone.utc) - hb).total_seconds() <= 45.0

    return _serialize_vehicle_state(row, online=online)


@router.get("/history")
def get_vehicle_telemetry_history(
    device_id: Optional[str] = Query(None),
    limit: int = Query(120, ge=1, le=500),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    _require_vehicle_read_access(user)
    norm_device = resolve_car_device_id(device_id, user.device_id)
    rows = (
        db.query(ObdTelemetryLog)
        .filter(ObdTelemetryLog.device_id == norm_device)
        .order_by(ObdTelemetryLog.recorded_at.desc())
        .limit(limit)
        .all()
    )
    items = [
        {
            "id": r.id,
            "recorded_at": _iso_utc(r.recorded_at),
            "fuel_level_liters": r.fuel_level_liters,
            "fuel_level_percent": r.fuel_level_percent,
            "estimated_range_km": r.estimated_range_km,
            "total_odometer_km": r.total_odometer_km,
            "battery_soc": r.battery_soc,
            "battery_voltage": r.battery_voltage,
            "battery_power_kw": r.battery_power_kw,
            "aux_12v_voltage": r.aux_12v_voltage,
            "speed_kmh": r.speed_kmh,
            "engine_rpm": r.engine_rpm,
            "coolant_temp": r.coolant_temp,
            "ambient_air_temp": r.ambient_air_temp,
        }
        for r in rows
    ]
    return {"device_id": norm_device, "count": len(items), "items": items}


@router.get("/fuel-history")
def get_vehicle_fuel_history(
    device_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> Dict[str, Any]:
    """Returns median-smoothed fuel level trend over the past [days] days (default 30),
    filtering out raw tank-slosh glitches and tagging refueling jumps."""
    from datetime import timedelta
    from statistics import median

    _require_vehicle_read_access(user)
    norm_device = resolve_car_device_id(device_id, user.device_id)
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        db.query(
            ObdTelemetryLog.recorded_at,
            ObdTelemetryLog.fuel_level_liters,
            ObdTelemetryLog.fuel_level_percent,
        )
        .filter(
            ObdTelemetryLog.device_id == norm_device,
            ObdTelemetryLog.recorded_at >= since,
            ObdTelemetryLog.fuel_level_liters.isnot(None),
        )
        .order_by(ObdTelemetryLog.recorded_at.asc())
        .all()
    )

    # Bucket into 2-hour windows (JST) and take the median of each bucket to eliminate float slosh
    jst = timezone(timedelta(hours=9))
    buckets: Dict[str, List[Tuple[datetime, float, float]]] = {}
    for rec_at, liters, pct in rows:
        if rec_at is None or liters is None or liters < 0 or liters > 45.0:
            continue
        dt_utc = rec_at if rec_at.tzinfo is not None else rec_at.replace(tzinfo=timezone.utc)
        dt_jst = dt_utc.astimezone(jst)
        b_hour = (dt_jst.hour // 2) * 2
        b_key = f"{dt_jst.year:04d}-{dt_jst.month:02d}-{dt_jst.day:02d}T{b_hour:02d}"
        eff_pct = (
            float(pct)
            if pct is not None and 0.0 <= pct <= 100.0
            else round(max(0.0, min(100.0, (float(liters) / YARIS_TANK_LITERS) * 100.0)), 1)
        )
        buckets.setdefault(b_key, []).append((dt_utc, float(liters), eff_pct))

    points: List[Dict[str, Any]] = []
    prev_liters: Optional[float] = None
    for b_key in sorted(buckets.keys()):
        samples = buckets[b_key]
        med_liters = round(median(s[1] for s in samples), 2)
        med_pct = round(median(s[2] for s in samples), 1)
        mid_ts = samples[len(samples) // 2][0]

        # Reject isolated single-sample downward glitch (>6L drop) if single sample in bucket
        if len(samples) == 1 and prev_liters is not None and (prev_liters - med_liters) > 6.0:
            continue

        delta_l = round(med_liters - prev_liters, 2) if prev_liters is not None else 0.0
        refueled = prev_liters is not None and delta_l >= 4.0
        points.append(
            {
                "recorded_at": _iso_utc(mid_ts),
                "fuel_level_liters": med_liters,
                "fuel_level_percent": med_pct,
                "sample_count": len(samples),
                "refueled": refueled,
                "delta_liters": delta_l,
            }
        )
        prev_liters = med_liters

    return {
        "device_id": norm_device,
        "days": days,
        "count": len(points),
        "items": points,
    }
