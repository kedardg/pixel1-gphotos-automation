"""NAS folder scanner and watchdog file monitor."""

import hashlib
import logging
import os
import re
from pathlib import Path

from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer

from .config import EXCLUDE_PATTERNS, SUPPORTED_EXTENSIONS, VIDEO_EXTENSIONS
from .state import StateDB

logger = logging.getLogger(__name__)


def device_filename(nas_path: str, original_name: str) -> str:
    """Generate a sanitized device filename with a hash prefix for uniqueness."""
    prefix = hashlib.sha256(nas_path.encode()).hexdigest()[:16]
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", original_name)
    safe_name = safe_name.lstrip(".-") or "unnamed"
    return f"{prefix}_{safe_name}"


def is_video(filename: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS


def _should_exclude(name: str) -> bool:
    for pattern in EXCLUDE_PATTERNS:
        if pattern.startswith("._") and name.startswith("._"):
            return True
        if name == pattern:
            return True
    return False


class NasScanner:
    def __init__(self, nas_photos_path: Path, state_db: StateDB) -> None:
        self._path = nas_photos_path.resolve()
        self._db = state_db

    def initial_scan(self) -> int:
        """Walk NAS recursively, insert new files into DB. Returns count of files found."""
        count = 0
        batch: list[tuple] = []

        for dirpath, dirnames, filenames in os.walk(self._path):
            # Filter excluded directories in-place
            dirnames[:] = [d for d in dirnames if not _should_exclude(d)]

            for name in filenames:
                if _should_exclude(name):
                    continue

                ext = os.path.splitext(name)[1].lower()
                if ext not in SUPPORTED_EXTENSIONS:
                    continue

                full_path = os.path.join(dirpath, name)

                # Skip symlinks
                if os.path.islink(full_path):
                    continue

                # Reject null bytes
                if "\x00" in full_path:
                    logger.warning("Skipping file with null byte: %r", full_path)
                    continue

                # Validate path is under nas_photos_path
                real_path = os.path.realpath(full_path)
                if not real_path.startswith(str(self._path)):
                    logger.warning("Path escaped NAS root: %s -> %s", full_path, real_path)
                    continue

                try:
                    stat = os.stat(full_path)
                except OSError:
                    continue

                dev_name = device_filename(full_path, name)
                batch.append((
                    full_path,
                    name,
                    dev_name,
                    stat.st_size,
                    stat.st_mtime,
                    int(is_video(name)),
                ))
                count += 1

                if count % 1000 == 0:
                    logger.info("Scanned %d files...", count)

        if batch:
            self._db.add_files(batch)

        logger.info("Initial scan complete: %d files found", count)
        return count


class WatchHandler(PatternMatchingEventHandler):
    """Writes new files to the state DB immediately on creation."""

    def __init__(self, nas_photos_path: Path, state_db: StateDB) -> None:
        patterns = [f"*{ext}" for ext in SUPPORTED_EXTENSIONS]
        super().__init__(
            patterns=patterns,
            ignore_patterns=["*.DS_Store", "._*"],
            ignore_directories=True,
            case_sensitive=False,
        )
        self._path = nas_photos_path.resolve()
        self._db = state_db

    def on_created(self, event) -> None:
        full_path = event.src_path

        if os.path.islink(full_path):
            return

        real_path = os.path.realpath(full_path)
        if not real_path.startswith(str(self._path)):
            return

        try:
            stat = os.stat(full_path)
        except OSError:
            return

        name = os.path.basename(full_path)
        dev_name = device_filename(full_path, name)

        self._db.add_files([(
            full_path,
            name,
            dev_name,
            stat.st_size,
            stat.st_mtime,
            int(is_video(name)),
        )])
        logger.info("New file detected: %s", name)


def start_observer(nas_photos_path: Path, state_db: StateDB) -> Observer:
    """Create and start a watchdog observer. Returns the started Observer."""
    handler = WatchHandler(nas_photos_path, state_db)
    observer = Observer()
    observer.schedule(handler, str(nas_photos_path), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info("Watchdog observer started on %s", nas_photos_path)
    return observer
