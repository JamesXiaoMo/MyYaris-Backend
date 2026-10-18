import logging
from sqlalchemy import text
from app.repositories.database import Base

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

            # Ensure indexes on favorites(user_id), favorites(folder_id), and favorite_folders(user_id) exist
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
            conn.commit()

            logger.info("Database schema migration completed successfully.")
        except Exception as e:
            logger.error(f"Error during schema migration: {e}", exc_info=True)

