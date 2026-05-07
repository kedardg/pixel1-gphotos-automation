# gphotos-backup

A daemon that uploads a local photo/video archive to Google Photos by cycling files through an Android device over ADB. Designed to leverage the unlimited "original quality" backup that older Pixel phones retain, without keeping the entire library on the phone.

## How it works

1. **Scan** a NAS/local directory for supported photo and video files.
2. **Push** a batch (default 8 GB) to the phone via `adb push` into `/sdcard/DCIM/GPhotosProxy/`.
3. **Trigger** a media scan and bring Google Photos to the foreground so its background sync uploads the batch.
4. **Wait** for upload to finish — monitors the phone's WiFi `tx_bytes` via `/proc/net/dev` and exits early when traffic goes idle, falling back to a fixed window otherwise.
5. **Delete** the pushed files from the phone and mark them uploaded in the local SQLite state DB.
6. **Repeat** until the archive is drained, then enter watch mode (`watchdog`) for new files.

The full pipeline state lives in a single SQLite database with a strict status state machine: `pending → on_device → presumed_uploaded`, with `failed` / `permanently_failed` branches and validated transitions.

## Features

- **Resumable**: state persists in SQLite, so daemon restarts pick up where they left off.
- **Smart upload detection**: tx-byte monitoring exits the wait window early when uploads are clearly done, instead of always burning the full 4h/12h timer.
- **Large video splitting**: videos above 3.7 GB are losslessly split with `ffmpeg -c copy -f segment` so they fit in Google Photos' per-file limit.
- **Crash recovery**: on startup, files left in `on_device` past the upload window are reconciled.
- **Singleton guard**: `fcntl.flock()` on a lock file prevents multiple daemons from racing on the same DB.
- **Graceful shutdown**: SIGTERM/SIGINT drain the in-flight batch before exiting.
- **Watch mode**: after the initial archive drains, a `watchdog` observer picks up new files dropped into the source directory.

## Requirements

- macOS or Linux
- Python ≥ 3.12
- `adb` (Android Platform Tools)
- `ffmpeg` (only needed if any video exceeds 3.7 GB)
- An Android device with USB debugging enabled and Google Photos installed/signed in
- Source directory readable by the daemon

## Install

```bash
git clone <this repo>
cd gphotos-backup
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Configure

Copy `config.example.json` to `config.json` and edit:

```json
{
  "nas_photos_path": "/path/to/your/photos",
  "adb_binary": "/opt/homebrew/bin/adb",
  "device_folder": "/sdcard/DCIM/GPhotosProxy",
  "batch_size_bytes": 8589934592,
  "photo_upload_window_seconds": 14400,
  "video_upload_window_seconds": 43200
}
```

`config.json` is gitignored — it stays local.

## Run

```bash
.venv/bin/python -m gphotos_backup
```

Logs go to `logs/daemon.log` (rotated at 10 MB × 5 files) and stderr.

### Status

```bash
.venv/bin/python -m gphotos_backup --status
```

Prints a summary of total / pending / on-device / uploaded / failed counts.

### Run as a launchd service (macOS)

A sample `com.gphotos-backup.daemon.plist` is included. Edit paths, then:

```bash
cp com.gphotos-backup.daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gphotos-backup.daemon.plist
```

## Project layout

```
gphotos_backup/
  __main__.py   # CLI entry, lock guard, --status
  config.py     # typed config + constants
  daemon.py     # main loop: archive mode, watch mode, batch cycle
  scanner.py    # initial NAS scan + watchdog observer
  state.py      # SQLite wrapper, FileStatus state machine
  adb.py        # ADB client (push, delete, free space, tx_bytes)
  splitter.py   # ffmpeg-based video splitting for files > 3.7 GB
```

## Notes

- The phone needs to stay plugged in, on WiFi, with Google Photos installed and signed in. The daemon foregrounds Google Photos itself so background-sync limits don't stall progress.
- Only one daemon can run per state DB — enforced via `fcntl.flock()`.
- Files are matched on the device by a hash-based filename (`device_filename` in the DB), so source-side renames don't break the pipeline.
- The daemon is read-only with respect to your source files: no renames, no rewrites. It just reads, copies, and tracks.

## License

MIT.
