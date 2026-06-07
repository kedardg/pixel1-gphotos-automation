# Geotag tooling

One-off pipeline to add GPS to GPS-less photos before uploading, by reading the
*visual cues* in each photo (recognisable landmarks) and tagging per-segment —
instead of dropping a single blanket pin on a whole folder.

Privacy: `make_sheets.py` blurs faces (OpenCV Haar cascades: frontal + profile +
flipped profile, generous margin) on downscaled copies **before** any image is
viewed. Originals are never shown unblurred and are only modified by exiftool to
add GPS.

## Files
- `make_sheets.py` — build face-blurred contact sheets ordered by capture time,
  plus `index.json` (idx → path → datetime).
  `python make_sheets.py SRC_DIR OUT_DIR`
- `apply_geotags.py` — read a segment plan, assign each JPG a coord by its idx
  range, propagate to sidecar RAW (same filename stem) and time-adjacent video,
  then write GPS in place via `exiftool -overwrite_original_in_place`. Groups by
  coordinate so it's only a few exiftool calls. Has `--dry-run`.
- `plan.example.json` — the segment-plan schema (placeholder values).

A real run's plans and index maps are personal location data, so they live in a
gitignored `_local/` folder, not in the repo.

## Workflow
```
# 1. build blurred contact sheets (faces blurred before viewing)
python make_sheets.py "/path/to/folder" out/

# 2. view out/sheet_*.jpg, note where the location changes by tile index,
#    and write a plan (see plan.example.json) mapping idx ranges -> lat/lon.

# 3. preview, then apply
python apply_geotags.py --index out/index.json --plan myplan.json \
  --folder "/path/to/folder" --dry-run
python apply_geotags.py --index out/index.json --plan myplan.json \
  --folder "/path/to/folder"
```

A segment may carry a `route` polyline (+ `roundtrip`) to spread frames evenly
along a path — useful for boat/ferry/train legs where the camera is moving.

## Requires
`exiftool`, and `opencv-python-headless` for the face blur.
