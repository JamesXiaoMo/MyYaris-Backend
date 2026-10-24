import logging
from sqlalchemy import text
from app.repositories.database import Base
import app.repositories.models  # noqa: F401 - ensure all models are registered on Base.metadata

logger = logging.getLogger(__name__)


def run_db_migrations(engine) -> None:
    """
    Safely migrates existing database schema to include new tables and columns
    without data loss or breaking backward compatibility.
    """
    logger.info("Checking database schema and applying migrations...")

    # 1. Create any missing tables (users, user_sessions, user_device_bindings, etc.)
    Base.metadata.create_all(bind=engine)

    # 2. Check and migrate SQLite columns for 'favorites' table
    with engine.connect() as conn:
        try:
            # Check if favorites table exists
            table_check = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='favorites'")
            ).fetchone()

            if table_check:
                # Inspect existing columns
                columns = [
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(favorites)")).fetchall()
                ]

                # Migrate user_id column
                if "user_id" not in columns:
                    logger.info("Migrating favorites table: Adding user_id column")
                    conn.execute(text("ALTER TABLE favorites ADD COLUMN user_id VARCHAR(64)"))
                    conn.commit()

                # Migrate is_legacy column
                if "is_legacy" not in columns:
                    logger.info("Migrating favorites table: Adding is_legacy column")
                    conn.execute(
                        text("ALTER TABLE favorites ADD COLUMN is_legacy BOOLEAN DEFAULT 0")
                    )
                    conn.commit()

                # Migrate folder_id column
                if "folder_id" not in columns:
                    logger.info("Migrating favorites table: Adding folder_id column")
                    conn.execute(
                        text("ALTER TABLE favorites ADD COLUMN folder_id VARCHAR(64)")
                    )
                    conn.commit()

                # Mark legacy entries
                conn.execute(
                    text("UPDATE favorites SET is_legacy = 1 WHERE user_id IS NULL AND is_legacy = 0")
                )
                conn.commit()

            # 3. Check and migrate SQLite columns & unique constraints for 'favorite_folders' table
            folder_table_check = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='favorite_folders'")
            ).fetchone()

            if folder_table_check:
                folder_cols = [
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(favorite_folders)")).fetchall()
                ]
                if "owner_scope" not in folder_cols:
                    logger.info("Migrating favorite_folders table: Adding owner_scope column")
                    conn.execute(
                        text("ALTER TABLE favorite_folders ADD COLUMN owner_scope VARCHAR(64) DEFAULT '__legacy__'")
                    )
                    conn.commit()

                if "name_norm" not in folder_cols:
                    logger.info("Migrating favorite_folders table: Adding name_norm column")
                    conn.execute(
                        text("ALTER TABLE favorite_folders ADD COLUMN name_norm VARCHAR(64) DEFAULT ''")
                    )
                    conn.commit()

                # Backfill owner_scope and name_norm for any existing rows
                conn.execute(
                    text(
                        """
                        UPDATE favorite_folders
                        SET owner_scope = CASE
                            WHEN user_id IS NOT NULL AND is_legacy = 0 THEN 'user:' || user_id
                            ELSE '__legacy__'
                        END,
                        name_norm = lower(trim(name)),
                        name = trim(name)
                        WHERE owner_scope IS NULL OR owner_scope = '' OR name_norm IS NULL OR name_norm = ''
                        """
                    )
                )
                conn.commit()

            # Ensure indexes and unique constraints on favorites and favorite_folders exist
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_favorites_user_id ON favorites (user_id)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_favorites_folder_id ON favorites (folder_id)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_favorite_folders_user_id ON favorite_folders (user_id)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_favorite_folders_name ON favorite_folders (name)")
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_favorite_folders_scope_name_norm "
                    "ON favorite_folders (owner_scope, name_norm)"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_favorite_folders_user_expr "
                    "ON favorite_folders (coalesce(user_id, '__legacy__'), is_legacy, lower(trim(name)))"
                )
            )
            conn.commit()

            # 4. Check and migrate SQLite columns for 'car_device_status' table
            car_status_check = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='car_device_status'")
            ).fetchone()

            if car_status_check:
                car_cols = [
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(car_device_status)")).fetchall()
                ]
                new_columns = [
                    ("applied_mode", "VARCHAR(32) DEFAULT 'list_loop'"),
                    ("requested_mode", "VARCHAR(32) DEFAULT 'list_loop'"),
                    ("queue_id", "VARCHAR(64)"),
                    ("queue_version", "INTEGER DEFAULT 1"),
                    ("queue_source", "VARCHAR(32) DEFAULT 'netease_favorite'"),
                    ("current_index", "INTEGER DEFAULT 0"),
                    ("consecutive_failures", "INTEGER DEFAULT 0"),
                    ("error_code", "VARCHAR(64)"),
                    ("error_detail", "TEXT"),
                    ("capabilities_json", "TEXT"),
                    ("status_seq", "INTEGER DEFAULT 0"),
                    ("last_applied_command_id", "VARCHAR(64)"),
                ]
                for col_name, col_def in new_columns:
                    if col_name not in car_cols:
                        logger.info(f"Migrating car_device_status table: Adding {col_name} column")
                        conn.execute(text(f"ALTER TABLE car_device_status ADD COLUMN {col_name} {col_def}"))
                        conn.commit()

            logger.info("Core database schema migration completed successfully.")
        except Exception as e:
            logger.error(f"Error during schema migration: {e}", exc_info=True)

    _migrate_offline_schema(engine)


def _migrate_offline_schema(engine):
    """Atomic, fail-closed SQLite upgrade for offline work and historical receipts."""
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            # Offline consistency upgrade. create_all alone cannot add columns to
            # the rejected baseline tables. Only the first upgrade fences old leases
            # and unverified historical saved entries; repeated runs preserve state.
            offline_columns = {
                "offline_jobs": [("skipped", "INTEGER NOT NULL DEFAULT 0")],
                "offline_items": [
                    ("attempts", "INTEGER NOT NULL DEFAULT 0"),
                    ("lease_token", "VARCHAR(64)"),
                    ("work_kind", "VARCHAR(16) NOT NULL DEFAULT 'download'"),
                    ("device_generation", "INTEGER NOT NULL DEFAULT 1"),
                    ("inventory_version", "INTEGER NOT NULL DEFAULT 0"),
                ],
                "offline_tracks": [("status", "VARCHAR(16) NOT NULL DEFAULT 'revoked'")],
            }
            upgrading = False
            for table, additions in offline_columns.items():
                cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
                for name, definition in additions:
                    if name not in cols:
                        upgrading = True
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
            for table in ("offline_jobs", "offline_items", "network_authorizations", "offline_tracks"):
                conn.execute(text(f"UPDATE {table} SET account_id = '__legacy__' WHERE account_id IS NULL"))
            if upgrading:
                conn.execute(text("UPDATE offline_items SET status='revoked', error_code='REVERIFY_REQUIRED', "
                                  "version=version+1, claim_deadline=NULL, lease_token=NULL, last_verified_at=NULL "
                                  "WHERE status IN ('claimed', 'saved')"))
                conn.execute(text("UPDATE offline_jobs SET state=CASE WHEN state='cancelled' THEN state ELSE 'paused' END, "
                                  "revision=revision+1, done=0, failed=(SELECT count(*) FROM offline_items "
                                  "WHERE offline_items.job_id=offline_jobs.job_id AND status IN ('failed','revoked'))"))
                conn.execute(text("UPDATE offline_tracks SET status='revoked', last_verified_at=NULL, version=version+1"))
                conn.execute(text("UPDATE network_authorizations SET batch_remaining=0, expires_at=CURRENT_TIMESTAMP"))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
