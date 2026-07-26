# GoPro redate + geotag

Fixes the two things a GoPro with an unset clock gets wrong before upload:
a bogus capture date and missing GPS.

`redate_geotag.py`:

- rewrites the **date** of every clip's timestamps to `--date` while **keeping
  each clip's original time-of-day**, so clips stay ordered within the day
  (`--fixed-time HH:MM:SS` forces one moment instead);
- sets GPS to `--lat/--lon` (default **O'ahu center**, 21.4389, -157.9973) on any
  clip missing coordinates (`--force-gps` overwrites existing ones too);
- also sets `FileModifyDate` so Finder/Photos agree.

By default targets `*.mp4` recursively, so `100GOPRO/` and `100GOPRO/fav/` are
both covered. `--media photo` processes GoPro stills (`.jpg/.heic/.dng/...`)
instead — images get EXIF date + `GPSLatitude/Longitude` tags (videos get the
QuickTime date atoms + `GPSCoordinates`); `--media both` does both in one pass.
`--sidecars` also touches `.lrv`/`.thm` (not normally needed — those aren't
uploaded). Writes in place with `-overwrite_original_in_place`.

## Workflow

```bash
# preview
.venv/bin/python tools/gopro/redate_geotag.py \
  "/path/to/100GOPRO" --date 2025:07:19 --dry-run

# apply (redate to 19 Jul 2025, keep time-of-day, stamp O'ahu where GPS missing)
.venv/bin/python tools/gopro/redate_geotag.py \
  "/path/to/100GOPRO" --date 2025:07:19

# same for the GoPro stills
.venv/bin/python tools/gopro/redate_geotag.py \
  "/path/to/100GOPRO" --date 2025:07:19 --media photo
```

Run this **after** de-duplication — redating changes bytes, so doing it first
would split otherwise-identical copies and defeat the hash-based dedup.

## Requires

`exiftool`.
