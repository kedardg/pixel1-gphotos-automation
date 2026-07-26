#!/usr/bin/env python3
"""Redate GoPro clips to a target date and stamp them with a location.

GoPro cameras whose clock was never set write a wrong capture date (e.g. this
O'ahu batch all read 2026:07:11, a year off) and no GPS at all. This tool:

  * rewrites the DATE portion of every clip's timestamps to --date while
    KEEPING each clip's original time-of-day, so the clips stay in the same
    order within the day (use --fixed-time HH:MM:SS to force one moment);
  * sets GPS to --lat/--lon (default O'ahu) on any clip missing coordinates
    (--force-gps overwrites existing coords too);
  * also matches FileModifyDate so Finder/Photos agree.

Targets *.mp4 recursively (so 100GOPRO/ and 100GOPRO/fav/ are both covered).
Add --sidecars to also touch .lrv/.thm. Skips nothing silently -- prints a plan.

Usage:
  redate_geotag.py DIR --date 2025:07:19 --dry-run
  redate_geotag.py DIR --date 2025:07:19
  redate_geotag.py DIR --date 2025:07:19 --lat 21.4389 --lon -157.9973
"""
import os, sys, json, argparse, subprocess

VID = (".mp4",)
SIDE = (".lrv", ".thm")
IMG = (".jpg", ".jpeg", ".heic", ".png", ".dng", ".gpr")


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def list_files(folder, media, sidecars):
    exts = ()
    if media in ("video", "both"):
        exts += VID + (SIDE if sidecars else ())
    if media in ("photo", "both"):
        exts += IMG
    out = []
    for dirpath, _, names in os.walk(folder):
        for n in names:
            if n.startswith("._"):
                continue
            if n.lower().endswith(exts):
                out.append(os.path.join(dirpath, n))
    return sorted(out)


def read_meta(paths):
    """{abspath: {'time': 'HH:MM:SS', 'has_gps': bool}}"""
    info = {}
    CH = 200
    for i in range(0, len(paths), CH):
        chunk = paths[i:i + CH]
        # No QuickTimeUTC: read/write the QuickTime date atoms literally (no tz
        # round-trip), so the stamped date lands exactly on --date.
        r = sh(["exiftool", "-j",
                "-d", "%H:%M:%S",
                "-DateTimeOriginal", "-CreateDate", "-GPSLatitude", "-GPSCoordinates", *chunk])
        try:
            data = json.loads(r.stdout) if r.stdout.strip() else []
        except json.JSONDecodeError:
            data = []
        for d in data:
            src = os.path.abspath(d.get("SourceFile", ""))
            info[src] = {
                "time": d.get("DateTimeOriginal") or d.get("CreateDate") or "",
                "has_gps": bool(d.get("GPSLatitude") or d.get("GPSCoordinates")),
            }
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--date", required=True, help="target date, YYYY:MM:DD")
    ap.add_argument("--fixed-time", help="force this HH:MM:SS instead of keeping each clip's")
    ap.add_argument("--lat", type=float, default=21.4389, help="default: O'ahu center")
    ap.add_argument("--lon", type=float, default=-157.9973)
    ap.add_argument("--force-gps", action="store_true", help="overwrite existing GPS too")
    ap.add_argument("--sidecars", action="store_true", help="also touch .lrv/.thm")
    ap.add_argument("--media", choices=("video", "photo", "both"), default="video",
                    help="which media to process (default video)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = list_files(args.folder, args.media, args.sidecars)
    info = read_meta(files)
    latref = "N" if args.lat >= 0 else "S"
    lonref = "E" if args.lon >= 0 else "W"

    n_date = n_gps = 0
    print(f"folder: {args.folder}  files: {len(files)}  ->  date {args.date}")
    for p in files:
        ap_ = os.path.abspath(p)
        meta = info.get(ap_, {"time": "", "has_gps": False})
        t = args.fixed_time or meta["time"] or "12:00:00"
        stamp = f"{args.date} {t}"
        set_gps = args.force_gps or not meta["has_gps"]
        is_video = os.path.splitext(p)[1].lower() in VID

        cmd = ["exiftool", "-overwrite_original_in_place",
               f"-AllDates={stamp}", f"-FileModifyDate={stamp}"]
        if is_video:
            cmd += [f"-QuickTime:CreateDate={stamp}", f"-QuickTime:ModifyDate={stamp}",
                    f"-TrackCreateDate={stamp}", f"-TrackModifyDate={stamp}",
                    f"-MediaCreateDate={stamp}", f"-MediaModifyDate={stamp}"]
        if set_gps:
            cmd += [f"-GPSLatitude={abs(args.lat)}", f"-GPSLatitudeRef={latref}",
                    f"-GPSLongitude={abs(args.lon)}", f"-GPSLongitudeRef={lonref}"]
            if is_video:
                cmd.append(f"-GPSCoordinates={args.lat} {args.lon}")
            n_gps += 1
        n_date += 1
        cmd.append(p)

        print(f"  {os.path.relpath(p, args.folder):30s} date={stamp}"
              + (f"  +GPS {args.lat},{args.lon}" if set_gps else "  (gps kept)"))
        if not args.dry_run:
            r = sh(cmd)
            if r.returncode != 0:
                sys.stderr.write(r.stderr)

    print(f"\n{'[dry-run] ' if args.dry_run else ''}redated {n_date} files, "
          f"geotagged {n_gps}")


if __name__ == "__main__":
    main()
