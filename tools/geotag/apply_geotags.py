#!/usr/bin/env python3
"""Apply GPS tags to GPS-less photos/videos using a per-city segment plan.

Anchor = JPGs in chronological order (from index.json). Each JPG's idx maps to a
segment -> (lat, lon). Sidecar RAW/other files sharing a JPG's filename stem inherit
its coords; standalone videos/raws inherit the nearest JPG by capture time.

Writes in place (-overwrite_original_in_place). Skips files that already have GPS.

Usage:
  apply_geotags.py --index index.json --plan plan.json --folder DIR [--dry-run]

plan.json:
{
  "default": {"place": "...", "lat": 0.0, "lon": 0.0},
  "segments": [{"from": 70, "to": 84, "place": "...", "lat": .., "lon": ..}]
}
"""
import os, sys, json, subprocess, argparse, collections

IMG_EXT = (".jpg", ".jpeg", ".rw2", ".dng", ".tif", ".tiff", ".png", ".heic")
VID_EXT = (".mp4", ".mov", ".m4v", ".avi")
ALL_EXT = IMG_EXT + VID_EXT


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def list_media(folder):
    out = []
    for root, _, names in os.walk(folder):
        for n in names:
            if n.startswith("._"):
                continue
            if n.lower().endswith(ALL_EXT):
                out.append(os.path.join(root, n))
    return out


def exif_times_and_gps(paths):
    """Return {abspath: {'dt': str, 'has_gps': bool}}."""
    info = {}
    CH = 400
    for i in range(0, len(paths), CH):
        chunk = paths[i:i + CH]
        r = sh(["exiftool", "-j", "-n", "-d", "%Y:%m:%d %H:%M:%S",
                "-DateTimeOriginal", "-CreateDate", "-GPSLatitude", *chunk])
        try:
            data = json.loads(r.stdout) if r.stdout.strip() else []
        except json.JSONDecodeError:
            data = []
        for d in data:
            src = os.path.abspath(d.get("SourceFile", ""))
            info[src] = {
                "dt": d.get("DateTimeOriginal") or d.get("CreateDate") or "",
                "has_gps": d.get("GPSLatitude") not in (None, ""),
            }
    return info


def _haversine(a, b):
    import math
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _point_on_route(route, frac):
    """Position at fractional distance `frac` (0..1) along a lat/lon polyline."""
    if len(route) == 1:
        return route[0]
    seglen = [_haversine(route[i], route[i+1]) for i in range(len(route)-1)]
    total = sum(seglen) or 1.0
    target = frac * total
    acc = 0.0
    for i, L in enumerate(seglen):
        if acc + L >= target:
            t = (target - acc) / L if L else 0.0
            la = route[i][0] + (route[i+1][0]-route[i][0]) * t
            lo = route[i][1] + (route[i+1][1]-route[i][1]) * t
            return (la, lo)
        acc += L
    return route[-1]


def build_route_overrides(index, plan):
    """idx -> (lat,lon,place) for segments that carry a 'route' polyline.
    Frames in the range are spread along the route (round-trip if requested),
    ordered by their chronological position in `index`."""
    overrides = {}
    order = [rec["idx"] for rec in index]  # chronological
    for seg in plan["segments"]:
        if "route" not in seg:
            continue
        route = [tuple(p) for p in seg["route"]]
        if seg.get("roundtrip"):
            route = route + route[::-1]
        ids = [i for i in order if seg["from"] <= i <= seg["to"]]
        n = len(ids)
        for k, i in enumerate(ids):
            frac = k / (n - 1) if n > 1 else 0.0
            la, lo = _point_on_route(route, frac)
            overrides[i] = (round(la, 6), round(lo, 6), seg["place"])
    return overrides


def coord_for_idx(idx, plan, overrides):
    if idx in overrides:
        return overrides[idx]
    for seg in plan["segments"]:
        if "route" in seg:
            continue
        if seg["from"] <= idx <= seg["to"]:
            return seg["lat"], seg["lon"], seg["place"]
    d = plan["default"]
    return d["lat"], d["lon"], d["place"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    index = json.load(open(args.index))           # chronological jpgs
    plan = json.load(open(args.plan))
    overrides = build_route_overrides(index, plan)

    # idx -> jpg path ; jpg path -> (lat,lon,place)
    jpg_coord = {}
    jpg_by_time = []   # (dt, path, coord)
    for rec in index:
        p = os.path.abspath(rec["path"])
        c = coord_for_idx(rec["idx"], plan, overrides)
        jpg_coord[p] = c
        jpg_by_time.append((rec.get("datetime", "") or "", p, c))
    jpg_by_time.sort()

    media = [os.path.abspath(p) for p in list_media(args.folder)]
    info = exif_times_and_gps(media)

    # stem -> jpg coord (for sidecar inheritance)
    stem_coord = {}
    for p, c in jpg_coord.items():
        stem_coord[os.path.splitext(p)[0]] = c

    def nearest_jpg_coord(dt):
        if not jpg_by_time:
            return plan["default"]["lat"], plan["default"]["lon"], plan["default"]["place"]
        if not dt:
            return jpg_by_time[len(jpg_by_time)//2][2]
        best = min(jpg_by_time, key=lambda x: abs(_t(x[0]) - _t(dt)))
        return best[2]

    assignments = []   # (path, lat, lon, place, is_video)
    skipped_gps = 0
    for p in media:
        meta = info.get(p, {"dt": "", "has_gps": False})
        if meta["has_gps"]:
            skipped_gps += 1
            continue
        ext = os.path.splitext(p)[1].lower()
        if p in jpg_coord:
            lat, lon, place = jpg_coord[p]
        elif os.path.splitext(p)[0] in stem_coord:
            lat, lon, place = stem_coord[os.path.splitext(p)[0]]
        else:
            lat, lon, place = nearest_jpg_coord(meta["dt"])
        assignments.append((p, lat, lon, place, ext in VID_EXT))

    # report
    by_place = collections.Counter(a[3] for a in assignments)
    print(f"folder: {args.folder}")
    print(f"media files: {len(media)} | already-geotagged (skipped): {skipped_gps} | "
          f"to tag: {len(assignments)}")
    for place, n in by_place.most_common():
        print(f"  {n:5d}  {place}")
    if args.dry_run:
        print("[dry-run] no files modified")
        return

    # group by (round coord, is_video) -> batch exiftool
    groups = collections.defaultdict(list)
    for p, lat, lon, place, isvid in assignments:
        groups[(round(lat, 6), round(lon, 6), isvid)].append(p)

    total = 0
    for (lat, lon, isvid), files in groups.items():
        latref = "N" if lat >= 0 else "S"
        lonref = "E" if lon >= 0 else "W"
        if isvid:
            base = ["exiftool", "-overwrite_original_in_place", "-api", "QuickTimeUTC=1",
                    f"-GPSCoordinates={lat} {lon}"]
        else:
            base = ["exiftool", "-overwrite_original_in_place",
                    f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={latref}",
                    f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={lonref}"]
        # chunk to keep arg list sane
        for i in range(0, len(files), 200):
            r = sh(base + files[i:i+200])
            sys.stdout.write(r.stdout.splitlines()[-1] + "\n" if r.stdout.strip() else "")
            if r.returncode != 0:
                sys.stderr.write(r.stderr)
        total += len(files)
    print(f"tagged {total} files")


def _t(s):
    """capture-time string -> sortable int; tolerate empties."""
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else 0


if __name__ == "__main__":
    main()
