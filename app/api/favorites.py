import uuid
from typing import Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.api.auth import require_user_or_phone_or_admin, verify_csrf
from app.repositories.database import get_db
from app.repositories.models import Favorite, FavoriteFolder, utc_now
from app.schemas.auth import AuthenticatedUser
from app.schemas.favorites import (
    FavoriteCreateRequest,
    FavoriteFolderCreateRequest,
    FavoriteFolderListResponse,
    FavoriteFolderResponse,
    FavoriteFolderUpdateRequest,
    FavoriteListResponse,
    FavoriteResponse,
    FavoriteUpdateRequest,
)

router = APIRouter(prefix="/api/v1/favorites", tags=["Favorites"])
folders_router = APIRouter(prefix="/api/v1/favorite-folders", tags=["Favorite Folders"])

_UNCATEGORIZED_SENTINELS = {"uncategorized", "__uncategorized__", "null", "none"}


def _is_same_owner_scope(
    record_user_id: Optional[str],
    record_is_legacy: bool,
    user: AuthenticatedUser,
) -> bool:
    """
    Checks whether a folder or favorite record belongs to the exact same account scope as the caller.
    - Logged-in account (user.user_id is not None): must match user_id and not be legacy.
    - Legacy token caller (user.user_id is None): must belong to the legacy pool (user_id is None or is_legacy is True).
    Cross-account references are strictly forbidden for all roles, including administrators.
    """
    if user.user_id:
        return record_user_id == user.user_id and not bool(record_is_legacy)
    return record_user_id is None or bool(record_is_legacy)


def _scope_favorites_query(query, user: AuthenticatedUser):
    if user.user_id:
        return query.filter(Favorite.user_id == user.user_id, Favorite.is_legacy.is_(False))
    return query.filter(or_(Favorite.user_id.is_(None), Favorite.is_legacy.is_(True)))


def _scope_folders_query(query, user: AuthenticatedUser):
    if user.user_id:
        return query.filter(FavoriteFolder.user_id == user.user_id, FavoriteFolder.is_legacy.is_(False))
    return query.filter(or_(FavoriteFolder.user_id.is_(None), FavoriteFolder.is_legacy.is_(True)))


def _get_owned_folder_or_404(db: Session, folder_id: str, user: AuthenticatedUser) -> FavoriteFolder:
    folder = db.query(FavoriteFolder).filter(FavoriteFolder.id == folder_id).first()
    if not folder or not _is_same_owner_scope(folder.user_id, folder.is_legacy, user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Favorite folder '{folder_id}' not found.",
        )
    return folder


def _check_favorite_access(row: Favorite, user: AuthenticatedUser) -> None:
    """Enforces strict account isolation: callers can only access favorites in their own account/legacy scope."""
    if not _is_same_owner_scope(row.user_id, row.is_legacy, user):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Favorite '{row.id}' not found.",
        )


def _to_folder_response(folder: FavoriteFolder, favorite_count: int = 0) -> FavoriteFolderResponse:
    return FavoriteFolderResponse(
        id=folder.id,
        name=folder.name,
        favorite_count=favorite_count,
        created_at=folder.created_at.isoformat() if folder.created_at else utc_now().isoformat(),
        updated_at=folder.updated_at.isoformat() if folder.updated_at else utc_now().isoformat(),
    )


def _to_favorite_response(row: Favorite) -> FavoriteResponse:
    folder_id = row.folder_id
    folder_name = None
    if folder_id and row.folder is not None:
        if row.folder.user_id == row.user_id and bool(row.folder.is_legacy) == bool(row.is_legacy):
            folder_name = row.folder.name
        else:
            folder_id = None
    elif folder_id and row.folder is None:
        folder_id = None

    return FavoriteResponse(
        id=row.id,
        folder_id=folder_id,
        folder_name=folder_name,
        name=row.name,
        address=row.address,
        latitude=row.latitude,
        longitude=row.longitude,
        coord_system=row.coord_system,
        created_at=row.created_at.isoformat() if row.created_at else utc_now().isoformat(),
        updated_at=row.updated_at.isoformat() if row.updated_at else utc_now().isoformat(),
    )


def _ensure_unique_folder_name(
    db: Session,
    name: str,
    user: AuthenticatedUser,
    exclude_folder_id: Optional[str] = None,
) -> None:
    dup_query = _scope_folders_query(db.query(FavoriteFolder), user).filter(
        func.lower(FavoriteFolder.name) == name.lower()
    )
    if exclude_folder_id:
        dup_query = dup_query.filter(FavoriteFolder.id != exclude_folder_id)
    if dup_query.first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Favorite folder '{name}' already exists.",
        )


# =====================================================================
# Favorite Folders CRUD (/api/v1/favorite-folders & /api/v1/favorites/folders)
# =====================================================================

@folders_router.get("", response_model=FavoriteFolderListResponse)
@router.get("/folders", response_model=FavoriteFolderListResponse)
def list_favorite_folders(
    limit: int = Query(default=50, ge=1, le=100, description="Items per page"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    sort_by: str = Query(default="created_at", pattern="^(created_at|updated_at|name)$", description="Sort field"),
    order: str = Query(default="asc", pattern="^(asc|desc)$", description="Sort direction"),
    search: Optional[str] = Query(default=None, max_length=64, description="Search keyword in folder name"),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """List single-level favorite folders belonging to the current account scope."""
    query = _scope_folders_query(db.query(FavoriteFolder), user)

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(FavoriteFolder.name.ilike(term))

    total = query.count()

    sort_col = getattr(FavoriteFolder, sort_by)
    if order == "desc":
        query = query.order_by(sort_col.desc(), FavoriteFolder.id.desc())
    else:
        query = query.order_by(sort_col.asc(), FavoriteFolder.id.asc())

    rows = query.offset(offset).limit(limit).all()

    # Compute favorite counts per folder within the same owner scope
    counts_by_folder: Dict[str, int] = {}
    if rows:
        folder_ids = [r.id for r in rows]
        count_rows = (
            _scope_favorites_query(
                db.query(Favorite.folder_id, func.count(Favorite.id)),
                user,
            )
            .filter(Favorite.folder_id.in_(folder_ids))
            .group_by(Favorite.folder_id)
            .all()
        )
        counts_by_folder = {fid: int(cnt) for fid, cnt in count_rows if fid}

    uncategorized_count = (
        _scope_favorites_query(db.query(Favorite), user)
        .filter(Favorite.folder_id.is_(None))
        .count()
    )

    items = [_to_folder_response(r, counts_by_folder.get(r.id, 0)) for r in rows]

    return FavoriteFolderListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(items)) < total,
        uncategorized_count=uncategorized_count,
    )


@folders_router.post("", response_model=FavoriteFolderResponse, status_code=status.HTTP_201_CREATED)
@router.post("/folders", response_model=FavoriteFolderResponse, status_code=status.HTTP_201_CREATED)
def create_favorite_folder(
    folder_in: FavoriteFolderCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """Create a single-level favorite folder for the current account scope."""
    verify_csrf(request, user)
    _ensure_unique_folder_name(db, folder_in.name, user)

    folder_id = f"fld_{uuid.uuid4().hex[:12]}"
    now = utc_now()

    folder = FavoriteFolder(
        id=folder_id,
        user_id=user.user_id,
        is_legacy=bool(user.user_id is None),
        name=folder_in.name,
        created_by=user.identity,
        created_at=now,
        updated_at=now,
    )
    db.add(folder)
    db.commit()
    db.refresh(folder)

    return _to_folder_response(folder, favorite_count=0)


@folders_router.get("/{folder_id}", response_model=FavoriteFolderResponse)
@router.get("/folders/{folder_id}", response_model=FavoriteFolderResponse)
def get_favorite_folder(
    folder_id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """Get a single favorite folder by ID with strict account isolation."""
    folder = _get_owned_folder_or_404(db, folder_id, user)
    fav_count = (
        _scope_favorites_query(db.query(Favorite), user)
        .filter(Favorite.folder_id == folder.id)
        .count()
    )
    return _to_folder_response(folder, favorite_count=fav_count)


@folders_router.patch("/{folder_id}", response_model=FavoriteFolderResponse)
@folders_router.put("/{folder_id}", response_model=FavoriteFolderResponse)
@router.patch("/folders/{folder_id}", response_model=FavoriteFolderResponse)
@router.put("/folders/{folder_id}", response_model=FavoriteFolderResponse)
def update_favorite_folder(
    folder_id: str,
    folder_in: FavoriteFolderUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """Rename a favorite folder with duplicate name validation within the same account scope."""
    verify_csrf(request, user)
    folder = _get_owned_folder_or_404(db, folder_id, user)
    _ensure_unique_folder_name(db, folder_in.name, user, exclude_folder_id=folder.id)

    folder.name = folder_in.name
    folder.updated_at = utc_now()
    db.commit()
    db.refresh(folder)

    fav_count = (
        _scope_favorites_query(db.query(Favorite), user)
        .filter(Favorite.folder_id == folder.id)
        .count()
    )
    return _to_folder_response(folder, favorite_count=fav_count)


@folders_router.delete("/{folder_id}")
@router.delete("/folders/{folder_id}")
def delete_favorite_folder(
    folder_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """
    Delete a favorite folder and atomically move all of its addresses back to uncategorized (folder_id = NULL)
    within the same database transaction. Never deletes user addresses.
    """
    verify_csrf(request, user)
    folder = _get_owned_folder_or_404(db, folder_id, user)

    now = utc_now()
    moved_count = (
        _scope_favorites_query(db.query(Favorite), user)
        .filter(Favorite.folder_id == folder.id)
        .update(
            {Favorite.folder_id: None, Favorite.updated_at: now},
            synchronize_session=False,
        )
    )
    db.delete(folder)
    db.commit()

    return {
        "status": "ok",
        "deleted_id": folder_id,
        "moved_to_uncategorized_count": int(moved_count),
        "message": "Favorite folder deleted successfully; addresses moved to uncategorized.",
    }


# =====================================================================
# Favorites CRUD (/api/v1/favorites)
# =====================================================================

@router.get("", response_model=FavoriteListResponse)
def list_favorites(
    limit: int = Query(default=50, ge=1, le=100, description="Items per page"),
    offset: int = Query(default=0, ge=0, description="Page offset"),
    sort_by: str = Query(default="created_at", pattern="^(created_at|updated_at|name)$", description="Sort field"),
    order: str = Query(default="desc", pattern="^(asc|desc)$", description="Sort direction"),
    search: Optional[str] = Query(default=None, max_length=100, description="Search keyword in name or address"),
    folder_id: Optional[str] = Query(
        default=None,
        max_length=64,
        description="Filter by folder ID, or 'uncategorized'/'null' for uncategorized items",
    ),
    uncategorized: Optional[bool] = Query(
        default=None,
        description="If true, only return uncategorized favorites (folder_id IS NULL)",
    ),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """
    List user favorite locations with strict account isolation and optional folder/uncategorized filtering.
    """
    query = _scope_favorites_query(db.query(Favorite), user)

    norm_folder_id = folder_id.strip() if isinstance(folder_id, str) and folder_id.strip() else None
    is_sentinel_uncat = norm_folder_id is not None and norm_folder_id.lower() in _UNCATEGORIZED_SENTINELS

    if uncategorized is True and norm_folder_id is not None and not is_sentinel_uncat:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot specify both uncategorized=true and a concrete folder_id.",
        )

    if uncategorized is True or is_sentinel_uncat:
        query = query.filter(Favorite.folder_id.is_(None))
    elif norm_folder_id is not None:
        folder = _get_owned_folder_or_404(db, norm_folder_id, user)
        query = query.filter(Favorite.folder_id == folder.id)
    elif uncategorized is False:
        query = query.filter(Favorite.folder_id.isnot(None))

    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.filter(or_(Favorite.name.ilike(term), Favorite.address.ilike(term)))

    total = query.count()

    sort_col = getattr(Favorite, sort_by)
    if order == "desc":
        query = query.order_by(sort_col.desc(), Favorite.id.desc())
    else:
        query = query.order_by(sort_col.asc(), Favorite.id.asc())

    rows = query.offset(offset).limit(limit).all()
    items = [_to_favorite_response(r) for r in rows]

    return FavoriteListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        has_more=(offset + len(items)) < total,
    )


@router.post("", response_model=FavoriteResponse, status_code=status.HTTP_201_CREATED)
def create_favorite(
    fav_in: FavoriteCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """
    Create a new favorite location with optional folder_id assignment.
    """
    verify_csrf(request, user)

    target_folder_id: Optional[str] = None
    if fav_in.folder_id is not None:
        folder = _get_owned_folder_or_404(db, fav_in.folder_id, user)
        target_folder_id = folder.id

    fav_id = f"fav_{uuid.uuid4().hex[:12]}"
    now = utc_now()

    row = Favorite(
        id=fav_id,
        user_id=user.user_id,
        is_legacy=bool(user.user_id is None),
        folder_id=target_folder_id,
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
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """Retrieve single favorite location with ownership isolation."""
    row = db.query(Favorite).filter(Favorite.id == favorite_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Favorite '{favorite_id}' not found.",
        )
    _check_favorite_access(row, user)
    return _to_favorite_response(row)


@router.patch("/{favorite_id}", response_model=FavoriteResponse)
@router.put("/{favorite_id}", response_model=FavoriteResponse)
def update_favorite(
    favorite_id: str,
    fav_in: FavoriteUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """Update favorite location or move between folder/uncategorized with strict ownership validation."""
    verify_csrf(request, user)
    row = db.query(Favorite).filter(Favorite.id == favorite_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Favorite '{favorite_id}' not found.",
        )
    _check_favorite_access(row, user)

    update_data = fav_in.model_dump(exclude_unset=True)
    if not update_data:
        return _to_favorite_response(row)

    if "folder_id" in update_data:
        if fav_in.folder_id is None or fav_in.folder_id.lower() in _UNCATEGORIZED_SENTINELS:
            row.folder_id = None
        else:
            folder = _get_owned_folder_or_404(db, fav_in.folder_id, user)
            if folder.user_id != row.user_id or bool(folder.is_legacy) != bool(row.is_legacy):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Favorite folder '{fav_in.folder_id}' not found.",
                )
            row.folder_id = folder.id

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
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_user_or_phone_or_admin),
):
    """Delete a favorite location with ownership validation."""
    verify_csrf(request, user)
    row = db.query(Favorite).filter(Favorite.id == favorite_id).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Favorite '{favorite_id}' not found.",
        )
    _check_favorite_access(row, user)

    db.delete(row)
    db.commit()

    return {
        "status": "ok",
        "deleted_id": favorite_id,
        "message": "Favorite deleted successfully.",
    }

