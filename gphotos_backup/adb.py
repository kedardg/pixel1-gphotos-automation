"""ADB subprocess helpers for communicating with the Pixel phone."""

import logging
import os
import signal
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class AdbClient:
    def __init__(self, adb_binary: Path, device_folder: str) -> None:
        self._adb_binary = str(adb_binary)
        self._device_folder = device_folder

    def _run(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        cmd = [self._adb_binary] + list(args)
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired as e:
            try:
                os.killpg(os.getpgid(e.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            raise

    def is_connected(self) -> bool:
        try:
            result = self._run("devices", timeout=10)
            lines = result.stdout.strip().splitlines()
            return any(line.endswith("\tdevice") for line in lines[1:])
        except subprocess.TimeoutExpired:
            logger.warning("adb devices timed out")
            return False

    def free_bytes(self) -> int:
        try:
            result = self._run("shell", "df", "/sdcard", timeout=10)
            if result.returncode != 0:
                return 0
            lines = result.stdout.strip().splitlines()
            if len(lines) < 2:
                return 0
            parts = lines[1].split()
            # df output: Filesystem 1K-blocks Used Available Use% Mounted on
            return int(parts[3]) * 1024
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            return 0

    def push_file(self, local_path: str, device_name: str) -> bool:
        device_path = f"{self._device_folder}/{device_name}"
        try:
            result = self._run("push", local_path, device_path, timeout=600)
            if result.returncode != 0:
                logger.error("adb push failed for %s: %s", local_path, result.stderr.strip())
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("adb push timed out for %s", local_path)
            return False

    def verify_push(self, device_name: str, expected_size: int) -> bool:
        device_path = f"{self._device_folder}/{device_name}"
        try:
            result = self._run("shell", "stat", "-c", "%s", "--", device_path, timeout=30)
            try:
                return result.returncode == 0 and int(result.stdout.strip()) == expected_size
            except (ValueError, TypeError):
                logger.warning("Unexpected stat output for %s: %r", device_name, result.stdout)
                return False
        except subprocess.TimeoutExpired:
            return False

    def delete_file(self, device_name: str) -> bool:
        device_path = f"{self._device_folder}/{device_name}"
        try:
            result = self._run("shell", "rm", "--", device_path, timeout=30)
            if result.returncode != 0:
                # File already gone is fine
                if "No such file" in result.stderr:
                    return True
                logger.warning("adb shell rm failed for %s: %s", device_name, result.stderr.strip())
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.error("adb shell rm timed out for %s", device_name)
            return False

    def ensure_device_folder(self) -> None:
        self._run("shell", "mkdir", "-p", self._device_folder, timeout=30)

    def trigger_media_scan(self) -> None:
        self._run(
            "shell", "am", "broadcast",
            "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
            "-d", f"file://{self._device_folder}",
            timeout=30,
        )

    def foreground_google_photos(self) -> None:
        self._run(
            "shell", "am", "start",
            "-n", "com.google.android.apps.photos/.home.HomeActivity",
            timeout=30,
        )

    def set_standby_bucket(self) -> None:
        self._run(
            "shell", "am", "set-standby-bucket",
            "com.google.android.apps.photos", "active",
            timeout=10,
        )

    def check_wifi_connected(self) -> bool:
        """Informational check — logs wifi status but does not gate execution."""
        try:
            result = self._run("shell", "dumpsys", "wifi", timeout=10)
            connected = "mWifiInfo" in result.stdout and "CONNECTED" in result.stdout
            if not connected:
                logger.info("Phone WiFi status: not connected (informational)")
            return connected
        except subprocess.TimeoutExpired:
            return True  # Assume connected if check fails

    def get_wifi_tx_bytes(self) -> int | None:
        """Read total wlan0 transmitted bytes from /proc/net/dev."""
        try:
            result = self._run("shell", "cat", "/proc/net/dev", timeout=10)
            if result.returncode != 0:
                return None
            for line in result.stdout.splitlines():
                if "wlan0:" in line:
                    parts = line.split()
                    return int(parts[9])  # tx_bytes is column 10 (0-indexed: 9)
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            pass
        return None

    def restart_server(self) -> None:
        """Kill and restart ADB server to prevent staleness over long runtimes."""
        logger.info("Restarting ADB server")
        try:
            self._run("kill-server", timeout=10)
        except subprocess.TimeoutExpired:
            pass
        try:
            self._run("start-server", timeout=10)
        except subprocess.TimeoutExpired:
            pass
