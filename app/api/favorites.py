import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.auth import require_phone_or_admin
from app.repositories.database import get_db
from app.repositories.models import Favorite, utc_now
from app.schemas.auth import AuthenticatedUser
from app.schemas.favorites import (
    FavoriteCreateRequest,
    FavoriteListResponse,
    FavoriteResponse,
    FavoriteUpdateRequest,
)

router = APIRouter(prefix="/api/v1/favorites", tags=["Favorites"])


def _to_favorite_response(row: Favorite) -> FavoriteResponse:
    return FavoriteResponse(
        id=row.id,
        name=row.name,
        address=row.address,
        latitude=row.latitude,
        longitude=row.longitude,
        coord_system=row.coord_system,
        created_at=row.created_at.isoformat() if row.created_at else utc_now().isoformat(),
        updated_at=row.updated_at.isoformat() if row.updated_at else utc_now().isoformat(),
    )


@router.get("", response_model=FavoriteListResponse)
def list_favorites(
    limit: int = Query(default=50, ge=1, le=100, description="Items per page"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    sort_by: str = Query(default="created_at", pattern="^(created_at|updated_at|name)$", description="Sort field"),
    order: str = Query(default="desc", pattern="^(asc|desc)$", description="Sort direction"),
    search: Optional[str] = Query(default=None, max_length=100, description="Search keyword in name or address"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    """
    List user favorite locations with pagination, sorting, and optional search.
    Requires phone or admin authentication.
    """
    query = db.query(Favorite)

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(or_(Favorite.name.ilike(term), Favorite.address.ilike(term)))

    total = query.count()

    sort_col = getattr(Favorite, sort_by)
    if order == "desc":
        query = query.order_by(sort_col.desc())
    else:
        query = query.order_by(sort_col.asc())

    rows = query.offset(offset).limit(limit).all()
    items = [_to_favorite_response(r) for r in rows]

    return FavoriteListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=FavoriteResponse, status_code=status.HTTP_201_CREATED)
def create_favorite(
    fav_in: FavoriteCreateRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    """
    Create a new favorite location.
    Requires phone or admin authentication.
    Strict coordinate range and non-empty address validation enforced.
    """
    fav_id = f"fav_{uuid.uuid4().hex[:12]}"
    now = utc_now()

    row = Favorite(
        id=fav_id,
        name=fav_in.name,
        address=fav_in.address,
        latitude=fav_in.latitude,
        longitude=fav_in.longitude,
        coord_system=fav_in.coord_system or "wgs84",
        created_by=user.identity,
        created_at=now,
        updated_at=now,
    )

    db.add(row)
    db.commit()
    db.refresh(row)

    return _to_favorite_response(row)


@router.get("/{favorite_id}", response_model=FavoriteResponse)
def get_favorite(
    favorite_id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    """
    Retrieve single favorite location by ID.
    Requires phone or admin authentication.
    """
    row = db.query(Favorite).filter(Favorite.id == favorite_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Favorite '{favorite_id}' not found.",
        )
    return _to_favorite_response(row)


@router.patch("/{favorite_id}", response_model=FavoriteResponse)
@router.put("/{favorite_id}", response_model=FavoriteResponse)
def update_favorite(
    favorite_id: str,
    fav_in: FavoriteUpdateRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    """
    Update an existing favorite location.
    Requires phone or admin authentication.
    """
    row = db.query(Favorite).filter(Favorite.id == favorite_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Favorite '{favorite_id}' not found.",
        )

    # Check which fields were explicitly provided in update request
    update_data = fav_in.model_dump(exclude_unset=True)
    if not update_data:
        return _to_favorite_response(row)

    if "name" in update_data:
        row.name = fav_in.name
    if "address" in update_data and fav_in.address is not None:
        row.address = fav_in.address
    if "latitude" in update_data and fav_in.latitude is not None:
        row.latitude = fav_in.latitude
    if "longitude" in update_data and fav_in.longitude is not None:
        row.longitude = fav_in.longitude
    if "coord_system" in update_data and fav_in.coord_system is not None:
        row.coord_system = fav_in.coord_system

    row.updated_at = utc_now()
    db.commit()
    db.refresh(row)

    return _to_favorite_response(row)


@router.delete("/{favorite_id}")
def delete_favorite(
    favorite_id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_phone_or_admin),
):
    """
    Delete a favorite location.
    Requires phone or admin authentication.
    Client must prompt for confirmation prior to calling this endpoint.
    """
    row = db.query(Favorite).filter(Favorite.id == favorite_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Favorite '{favorite_id}' not found.",
        )

    db.delete(row)
    db.commit()

    return {
        "status": "ok",
        "deleted_id": favorite_id,
        "message": "Favorite deleted successfully.",
    }
