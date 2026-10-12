#!/usr/bin/env python3
import datetime
import os
from pathlib import Path
import sqlite3
import sys

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings


def backup_database(retention_days: int = 7) -> str:
    db_url = settings.DATABASE_URL
    if not db_url.startswith("sqlite:///"):
        print(f"Non-SQLite database configured ({db_url}), skipping sqlite backup.")
        return ""

    source_path = Path(db_url[len("sqlite:///") :])
    if not source_path.exists():
        print(f"Database file {source_path} does not exist yet, skipping.")
        return ""

    backup_dir = Path(settings.DATA_DIR) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"myyaris_backup_{timestamp}.db"

    print(f"Starting SQLite backup from {source_path} -> {backup_file}...")
    src_conn = sqlite3.connect(source_path)
    dst_conn = sqlite3.connect(backup_file)
    with dst_conn:
        src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()

    print(f"Backup created successfully: {backup_file} ({backup_file.stat().st_size} bytes)")

    # Purge backups older than retention_days
    cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)
    purged_count = 0
    for f in backup_dir.glob("myyaris_backup_*.db"):
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
        if mtime < cutoff:
            f.unlink()
            purged_count += 1

    if purged_count > 0:
        print(f"Purged {purged_count} backups older than {retention_days} days.")

    return str(backup_file)


if __name__ == "__main__":
    backup_database()
