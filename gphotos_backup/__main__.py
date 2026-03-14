"""Entry point for gphotos-backup daemon."""

import argparse
import fcntl
import json
import sys
from pathlib import Path

from .config import (
    EXIT_ADB_MISSING,
    EXIT_CONFIG_ERROR,
    EXIT_LOCK_HELD,
    EXIT_NAS_MISSING,
    EXIT_OK,
    load_config,
)
from .daemon import Daemon, setup_logging
from .state import StateDB, open_readonly


def get_project_dir() -> Path:
    """Determine project directory (where config.json lives)."""
    return Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gphotos-backup",
        description="Pixel 1 Google Photos upload proxy daemon",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print current progress and exit",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output --status as JSON",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reset permanently_failed files to pending",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.json (default: project_dir/config.json)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_dir = get_project_dir()
    config_path = args.config or project_dir / "config.json"
    db_path = project_dir / "state.db"

    # --status: runs before flock, read-only
    if args.status:
        if not db_path.exists():
            print("No state database found. Run the daemon first.", file=sys.stderr)
            sys.exit(EXIT_CONFIG_ERROR)

        db = open_readonly(db_path)
        progress = db.get_progress()
        db.close()

        if args.json:
            print(json.dumps(progress, indent=2))
        else:
            print(f"Total:              {progress.get('total', 0)}")
            print(f"Pending:            {progress.get('pending', 0)}")
            print(f"On device:          {progress.get('on_device', 0)}")
            print(f"Presumed uploaded:  {progress.get('presumed_uploaded', 0)}")
            print(f"Failed:             {progress.get('failed', 0)}")
            print(f"Permanently failed: {progress.get('permanently_failed', 0)}")
        sys.exit(EXIT_OK)

    # --retry-failed: runs before flock
    if args.retry_failed:
        if not db_path.exists():
            print("No state database found.", file=sys.stderr)
            sys.exit(EXIT_CONFIG_ERROR)

        db = StateDB(db_path)
        db.init_db()
        count = db.reset_permanently_failed()
        db.close()
        print(f"Reset {count} permanently failed files to pending")
        sys.exit(EXIT_OK)

    # Acquire singleton lock
    lock_path = project_dir / "gphotos-backup.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Another instance is already running", file=sys.stderr)
        sys.exit(EXIT_LOCK_HELD)

    # Load config
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"Config file not found: {config_path}", file=sys.stderr)
        sys.exit(EXIT_CONFIG_ERROR)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG_ERROR)

    # Validate paths
    if not config.nas_photos_path.exists():
        print(f"NAS path not found: {config.nas_photos_path}", file=sys.stderr)
        sys.exit(EXIT_NAS_MISSING)
    if not config.adb_binary.exists():
        print(f"ADB binary not found: {config.adb_binary}", file=sys.stderr)
        sys.exit(EXIT_ADB_MISSING)

    # Setup logging
    setup_logging(project_dir / "logs")

    # Init state DB
    state_db = StateDB(db_path)
    state_db.init_db()

    # Create ADB client
    from .adb import AdbClient

    adb = AdbClient(config.adb_binary, config.device_folder)

    # Run daemon
    daemon = Daemon(config, state_db, adb, project_dir)
    try:
        daemon.run()
    finally:
        state_db.close()
        lock_file.close()

    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
