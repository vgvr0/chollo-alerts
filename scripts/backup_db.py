"""Create a consistent SQLite backup without stopping the application."""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Backup already exists: {destination}")
    with (
        sqlite3.connect(source) as source_db,
        sqlite3.connect(destination) as backup_db,
    ):
        source_db.backup(backup_db)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup seguro de la SQLite")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(os.getenv("DATABASE_PATH", "data/chollometro.sqlite3")),
        help="SQLite de origen (por defecto DATABASE_PATH o data/chollometro.sqlite3)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("backups"),
        help="Directorio de backups (por defecto backups)",
    )
    args = parser.parse_args()
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    destination = args.output_dir / f"chollometro-{timestamp}.sqlite3"
    backup_database(args.source, destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
