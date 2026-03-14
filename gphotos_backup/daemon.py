"""Main daemon loop, signal handling, and lifecycle management."""

import logging
import logging.handlers
import signal
import subprocess
import threading
from pathlib import Path

from .adb import AdbClient
from .config import (
    ADB_SERVER_RESTART_INTERVAL,
    CONNECTIVITY_CHECK_INTERVAL_SECONDS,
    DaemonConfig,
    MIN_DEVICE_FREE_BYTES,
    WATCH_MODE_BUFFER_SECONDS,
)
from .scanner import NasScanner, start_observer
from .state import FileStatus, StateDB

logger = logging.getLogger(__name__)


class GracefulShutdown:
    def __init__(self) -> None:
        self._event = threading.Event()
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum: int, frame) -> None:
        logger.info("Received %s, shutting down...", signal.Signals(signum).name)
        self._event.set()

    @property
    def should_exit(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout=timeout)


def notify_macos(title: str, message: str) -> None:
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    subprocess.run(
        ["osascript", "-e", script, title, message],
        check=False,
        capture_output=True,
    )


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "daemon.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Also log to stderr for development
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)


class Daemon:
    def __init__(
        self,
        config: DaemonConfig,
        state_db: StateDB,
        adb: AdbClient,
        project_dir: Path,
    ) -> None:
        self._config = config
        self._db = state_db
        self._adb = adb
        self._project_dir = project_dir
        self._shutdown = GracefulShutdown()
        self._cycle_count = 0

    def run(self) -> None:
        logger.info("Daemon starting")

        # Crash recovery
        self._crash_recovery()

        # Initial scan
        scanner = NasScanner(self._config.nas_photos_path, self._db)
        scanner.initial_scan()

        # Archive mode
        if self._db.has_pending_or_on_device():
            logger.info("Entering archive mode")
            self._archive_mode(scanner)

        # Watch mode
        if not self._shutdown.should_exit:
            logger.info("Entering watch mode")
            self._watch_mode(scanner)

        logger.info("Daemon exiting cleanly")

    def _crash_recovery(self) -> None:
        """Handle stale on_device files from previous run."""
        max_window = max(
            self._config.photo_upload_window_seconds,
            self._config.video_upload_window_seconds,
        )
        stale = self._db.get_on_device_stale(max_window)
        if not stale:
            return

        logger.info("Crash recovery: %d stale on_device files", len(stale))
        if self._adb.is_connected():
            for f in stale:
                self._adb.delete_file(f.device_filename)
                self._db.update_status(f.id, FileStatus.PRESUMED_UPLOADED)
                logger.info("Recovered stale file: %s", f.filename)
        else:
            logger.warning(
                "Phone not connected — %d stale on_device files will be handled when phone reconnects",
                len(stale),
            )

    def _archive_mode(self, scanner: NasScanner) -> None:
        while not self._shutdown.should_exit and self._db.has_pending_or_on_device():
            # Get next batch (photos first, then videos)
            batch = self._db.get_pending_batch(
                self._config.batch_size_bytes, is_video=False
            )
            if not batch:
                batch = self._db.get_pending_batch(
                    self._config.batch_size_bytes, is_video=True
                )
            if not batch:
                break

            # Pre-flight checks
            if not self._adb.is_connected():
                notify_macos("gphotos-backup", "Phone disconnected")
                logger.warning("Phone disconnected, waiting...")
                self._shutdown.wait(timeout=60)
                continue

            if self._adb.free_bytes() < MIN_DEVICE_FREE_BYTES:
                notify_macos("gphotos-backup", "Phone storage low")
                logger.warning("Phone storage low, waiting...")
                self._shutdown.wait(timeout=60)
                continue

            self._adb.check_wifi_connected()

            # Run batch cycle
            self._run_batch_cycle(batch)

        # Re-scan at archive→watch transition
        if not self._shutdown.should_exit:
            logger.info("Archive complete. Running transition re-scan...")
            scanner.initial_scan()

            progress = self._db.get_progress()
            logger.info(
                "[MODE] Archive complete. %d files processed. %d permanently failed. Entering watch mode.",
                progress.get("presumed_uploaded", 0),
                progress.get("permanently_failed", 0),
            )

    def _watch_mode(self, scanner: NasScanner) -> None:
        observer = start_observer(self._config.nas_photos_path, self._db)
        observer_restart_count = 0

        while not self._shutdown.should_exit:
            self._shutdown.wait(timeout=WATCH_MODE_BUFFER_SECONDS)

            if self._shutdown.should_exit:
                break

            # Health check observer
            if not observer.is_alive():
                observer_restart_count += 1
                logger.warning(
                    "Observer died, restarting (attempt %d)", observer_restart_count
                )
                if observer_restart_count > 5:
                    logger.error("Observer restart limit reached, falling back to periodic scan")
                    scanner.initial_scan()
                    observer_restart_count = 0
                    observer = start_observer(self._config.nas_photos_path, self._db)
                else:
                    observer = start_observer(self._config.nas_photos_path, self._db)

            # Process pending files
            if self._db.has_pending_or_on_device():
                batch = self._db.get_pending_batch(
                    self._config.batch_size_bytes, is_video=False
                )
                if not batch:
                    batch = self._db.get_pending_batch(
                        self._config.batch_size_bytes, is_video=True
                    )
                if batch and self._adb.is_connected():
                    self._run_batch_cycle(batch)

        # Cleanup
        observer.stop()
        observer.join(timeout=5)

    def _run_batch_cycle(self, batch: list) -> None:
        self._cycle_count += 1
        logger.info("Starting batch cycle #%d (%d files)", self._cycle_count, len(batch))

        # Periodic ADB server restart
        if self._cycle_count % ADB_SERVER_RESTART_INTERVAL == 0:
            self._adb.restart_server()

        self._adb.ensure_device_folder()
        self._adb.set_standby_bucket()

        # Push files
        pushed = []
        for file in batch:
            if self._shutdown.should_exit:
                break

            # Re-validate file before push
            file_path = Path(file.nas_path)
            if not file_path.exists() or file_path.is_symlink():
                logger.warning("File missing or is symlink at push time: %s", file.nas_path)
                self._db.increment_retry(file.id)
                continue

            real_path = file_path.resolve()
            if not str(real_path).startswith(str(self._config.nas_photos_path.resolve())):
                logger.warning("Path escaped NAS root at push time: %s", file.nas_path)
                self._db.increment_retry(file.id)
                continue

            success = self._adb.push_file(file.nas_path, file.device_filename)
            if success and self._adb.verify_push(file.device_filename, file.size_bytes):
                self._db.update_status(file.id, FileStatus.ON_DEVICE)
                pushed.append(file)
            else:
                self._db.increment_retry(file.id)

        if not pushed:
            logger.warning("No files successfully pushed in batch cycle #%d", self._cycle_count)
            return

        # Trigger media scan + foreground Google Photos
        self._adb.trigger_media_scan()
        self._adb.foreground_google_photos()

        # Wait for upload
        has_video = any(f.is_video for f in pushed)
        upload_window = (
            self._config.video_upload_window_seconds
            if has_video
            else self._config.photo_upload_window_seconds
        )
        self._wait_with_checks(upload_window)

        # Delete from phone
        for file in pushed:
            if self._shutdown.should_exit:
                break
            self._adb.delete_file(file.device_filename)
            self._db.update_status(file.id, FileStatus.PRESUMED_UPLOADED)

        # Maintenance
        if self._cycle_count % 5 == 0:
            self._db.checkpoint()

        self._log_progress()

    def _wait_with_checks(self, window_seconds: float) -> None:
        """Sleep in chunks, checking connectivity periodically."""
        elapsed = 0.0
        chunk = float(CONNECTIVITY_CHECK_INTERVAL_SECONDS)

        while elapsed < window_seconds and not self._shutdown.should_exit:
            remaining = min(chunk, window_seconds - elapsed)
            self._shutdown.wait(timeout=remaining)
            elapsed += remaining

            if self._shutdown.should_exit:
                break

            if not self._adb.is_connected():
                logger.warning("Phone disconnected during upload wait")
                notify_macos("gphotos-backup", "Phone disconnected during upload wait")
                break

            self._adb.set_standby_bucket()

    def _log_progress(self) -> None:
        progress = self._db.get_progress()
        logger.info(
            "[PROGRESS] Total: %d | Uploaded: %d | Pending: %d | On device: %d | "
            "Failed: %d | Perm failed: %d | Cycle #%d",
            progress.get("total", 0),
            progress.get("presumed_uploaded", 0),
            progress.get("pending", 0),
            progress.get("on_device", 0),
            progress.get("failed", 0),
            progress.get("permanently_failed", 0),
            self._cycle_count,
        )
