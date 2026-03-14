"""Typed configuration loader for gphotos-backup daemon."""

import json
from dataclasses import dataclass
from pathlib import Path

# Exit codes
EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_LOCK_HELD = 2
EXIT_DB_CORRUPT = 3
EXIT_NAS_MISSING = 4
EXIT_ADB_MISSING = 5

# Hardcoded constants (not user-configurable)
SUPPORTED_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif",
    ".tiff", ".tif", ".raw", ".cr2", ".nef", ".arw", ".dng",
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv", ".webm",
})

VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".wmv", ".webm",
})

EXCLUDE_PATTERNS = frozenset({
    ".DS_Store", "._*", "Thumbs.db", ".thumbnail", "@eaDir",
})

CONNECTIVITY_CHECK_INTERVAL_SECONDS = 900  # 15 min
WATCH_MODE_BUFFER_SECONDS = 1800  # 30 min
MIN_DEVICE_FREE_BYTES = 5 * 1024**3  # 5 GB
MAX_RETRIES = 3
ADB_SERVER_RESTART_INTERVAL = 10  # restart ADB server every N cycles


@dataclass
class DaemonConfig:
    nas_photos_path: Path
    adb_binary: Path
    device_folder: str = "/sdcard/DCIM/GPhotosProxy"
    batch_size_bytes: int = 8 * 1024**3  # 8 GB
    photo_upload_window_seconds: int = 14400  # 4 hours
    video_upload_window_seconds: int = 43200  # 12 hours

    def __post_init__(self) -> None:
        self.nas_photos_path = Path(self.nas_photos_path)
        self.adb_binary = Path(self.adb_binary)

    def validate(self) -> None:
        if not self.nas_photos_path.exists() or not self.nas_photos_path.is_dir():
            raise ValueError(f"nas_photos_path does not exist or is not a directory: {self.nas_photos_path}")
        if not self.adb_binary.exists() or not self.adb_binary.is_file():
            raise ValueError(f"adb_binary does not exist or is not a file: {self.adb_binary}")
        if not self.device_folder.startswith("/sdcard/"):
            raise ValueError(f"device_folder must start with /sdcard/: {self.device_folder}")
        if ".." in self.device_folder:
            raise ValueError(f"device_folder must not contain '..': {self.device_folder}")
        if self.batch_size_bytes <= 0:
            raise ValueError(f"batch_size_bytes must be positive: {self.batch_size_bytes}")
        if self.photo_upload_window_seconds <= 0:
            raise ValueError(f"photo_upload_window_seconds must be positive: {self.photo_upload_window_seconds}")
        if self.video_upload_window_seconds <= 0:
            raise ValueError(f"video_upload_window_seconds must be positive: {self.video_upload_window_seconds}")


def load_config(config_path: Path) -> DaemonConfig:
    with open(config_path) as f:
        data = json.load(f)

    config = DaemonConfig(**data)
    config.validate()
    return config
