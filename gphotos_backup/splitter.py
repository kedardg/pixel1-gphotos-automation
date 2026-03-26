"""Split large video files into chunks using ffmpeg stream copy."""

import logging
import subprocess
from pathlib import Path

from .config import MAX_FILE_SIZE_BYTES

logger = logging.getLogger(__name__)

# Target segment duration in seconds — ffmpeg will split at the nearest keyframe.
# 15 minutes per segment is conservative; actual size depends on bitrate.
_INITIAL_SEGMENT_SECONDS = 900


def needs_split(size_bytes: int, is_video: bool) -> bool:
    return is_video and size_bytes > MAX_FILE_SIZE_BYTES


def split_video(source_path: str, tmp_dir: Path) -> list[Path] | None:
    """Split a video into chunks <= MAX_FILE_SIZE_BYTES.

    Uses ffmpeg -c copy (no re-encoding) with segment muxer.
    Returns list of chunk paths on success, None on failure.
    """
    source = Path(source_path)
    suffix = source.suffix  # e.g. .mp4
    stem = source.stem
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(tmp_dir / f"{stem}_part%03d{suffix}")

    # Estimate segment duration from file size and target chunk size
    file_size = source.stat().st_size
    num_chunks = (file_size // MAX_FILE_SIZE_BYTES) + 1
    duration = _get_duration(source_path)
    if duration is None:
        logger.error("Could not determine duration of %s", source_path)
        return None

    segment_seconds = int(duration / num_chunks)
    # Ensure at least 60 seconds per segment
    segment_seconds = max(segment_seconds, 60)

    cmd = [
        "ffmpeg", "-y",
        "-i", source_path,
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        pattern,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            start_new_session=True,
        )
        if result.returncode != 0:
            logger.error("ffmpeg split failed for %s: %s", source_path, result.stderr[-500:])
            return None
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg split timed out for %s", source_path)
        return None

    # Collect output chunks
    chunks = sorted(tmp_dir.glob(f"{stem}_part*{suffix}"))
    if not chunks:
        logger.error("ffmpeg produced no output chunks for %s", source_path)
        return None

    # Verify all chunks are under the limit (with 5% tolerance for keyframe overshoot)
    tolerance = int(MAX_FILE_SIZE_BYTES * 1.05)
    for chunk in chunks:
        if chunk.stat().st_size > tolerance:
            logger.warning(
                "Chunk %s is %.1f MB (over limit), re-splitting may be needed",
                chunk.name,
                chunk.stat().st_size / 1024 / 1024,
            )

    logger.info("Split %s into %d chunks", source.name, len(chunks))
    return chunks


def _get_duration(path: str) -> float | None:
    """Get video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError):
        pass
    return None
