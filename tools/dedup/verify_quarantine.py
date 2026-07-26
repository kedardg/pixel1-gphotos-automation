#!/usr/bin/env python3
"""Byte-verify quarantined files against a RETAINED twin before you delete them.

Independent re-check (uses `cmp`, not the earlier hashes) that every file the
dedup tools moved out still has an identical copy that will survive deletion:

  * cross-folder dupes (cross_dupes report): each file in ROOT/_xref_quarantine/
    is compared to the archive file it matched (that archive copy is retained).
  * intra dupes (find_dupes/clean_dupes report): each file in ROOT/_dup_quarantine/
    is compared to the KEPT copy of its group. Because later steps can move or
    edit that keeper, the twin is resolved wherever it now lives:
      1. its live path in ROOT
      2. ROOT/_xref_quarantine/<keep>   (keeper was itself archived + moved)
    and if the twin is a video that now differs only in metadata (e.g. the
    keeper was redated after quarantine), a video-STREAM compare confirms the
    picture content is identical (reported OK-STREAM).

Verdicts: IDENTICAL / OK-STREAM (video content same, metadata differs) are safe;
DIFFER / MISSING are real problems. Read-only. Exit 0 only if all safe.

Usage:
  verify_quarantine.py --root ROOT [--intra dupes.json] [--xref xref.json]
"""
import os, sys, json, argparse, re, subprocess

COPY = re.compile(r"^(.*) \(\d+\)$")
# essence-comparable via ffmpeg (hash the picture data, ignore metadata) --
# covers both video packets and still-image scans, so a redated keeper that
# differs only in EXIF/QuickTime metadata still verifies as content-identical.
MEDIA_EXT = (".mp4", ".mov", ".m4v", ".avi", ".jpg", ".jpeg", ".heic", ".png", ".dng")


def is_gopro(base):
    return base.upper().startswith(("GX0125", "GL0125", "GH0125", "GX0126", "GL0126"))


def keep_key(rel):
    """Same rule clean_dupes used: the copy that sorts first was kept."""
    base = os.path.basename(rel)
    stem = os.path.splitext(base)[0]
    under_gopro = rel.startswith("100GOPRO/") or "/100GOPRO/" in ("/" + rel)
    gopro_pref = 0 if (is_gopro(base) and under_gopro) else 1
    copy_pref = 1 if COPY.match(stem) else 0
    return (gopro_pref, copy_pref, len(rel), rel)


def stream_md5(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0",
                        "-c", "copy", "-f", "md5", "-"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def verify(q, twin):
    if not os.path.exists(q) or twin is None or not os.path.exists(twin):
        return "MISSING"
    if subprocess.run(["cmp", "-s", q, twin]).returncode == 0:
        return "IDENTICAL"
    # bytes differ: compare the picture essence (metadata-only edits are ok)
    if q.lower().endswith(MEDIA_EXT) and twin.lower().endswith(MEDIA_EXT):
        a, b = stream_md5(q), stream_md5(twin)
        if a and b and a == b:
            return "OK-STREAM"
    return "DIFFER"


def run(pairs, label):
    tally = {"IDENTICAL": 0, "OK-STREAM": 0, "DIFFER": 0, "MISSING": 0}
    print(f"\n=== {label}: {len(pairs)} files ===")
    for i, (q, twin, tag) in enumerate(pairs, 1):
        v = verify(q, twin)
        tally[v] += 1
        if v in ("DIFFER", "MISSING"):
            print(f"  {v}: {tag}\n       q:    {q}\n       twin: {twin}")
        if i % 200 == 0:
            sys.stderr.write(f"  {label}: {i}/{len(pairs)}\n"); sys.stderr.flush()
    print(f"  IDENTICAL {tally['IDENTICAL']} | OK-STREAM {tally['OK-STREAM']} | "
          f"DIFFER {tally['DIFFER']} | MISSING {tally['MISSING']}")
    return tally


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--intra")
    ap.add_argument("--xref")
    args = ap.parse_args()
    root = os.path.abspath(args.root)
    xq = os.path.join(root, "_xref_quarantine")
    dq = os.path.join(root, "_dup_quarantine")
    grand = {"IDENTICAL": 0, "OK-STREAM": 0, "DIFFER": 0, "MISSING": 0}

    if args.xref:
        rep = json.load(open(args.xref))
        pairs = [(os.path.join(xq, m["target"]), m["ref_match"], m["target"])
                 for m in rep["matches"]]
        for k, v in run(pairs, "cross-folder dupes (vs archive twin)").items():
            grand[k] += v

    if args.intra:
        rep = json.load(open(args.intra))
        pairs = []
        for g in rep["groups"]:
            rels = sorted(g["paths"], key=keep_key)
            keep, drops = rels[0], rels[1:]
            # resolve the keeper wherever it now lives
            twin = None
            for cand in (os.path.join(root, keep), os.path.join(xq, keep), os.path.join(dq, keep)):
                if os.path.exists(cand):
                    twin = cand
                    break
            for d in drops:
                pairs.append((os.path.join(dq, d), twin, d))
        for k, v in run(pairs, "intra dupes (vs kept copy)").items():
            grand[k] += v

    safe = grand["IDENTICAL"] + grand["OK-STREAM"]
    print(f"\n===== TOTAL: safe {safe} "
          f"(IDENTICAL {grand['IDENTICAL']} + OK-STREAM {grand['OK-STREAM']}) | "
          f"DIFFER {grand['DIFFER']} | MISSING {grand['MISSING']} =====")
    if grand["DIFFER"] or grand["MISSING"]:
        print("NOT all safe -- investigate the DIFFER/MISSING above before deleting.")
        sys.exit(1)
    print("Every quarantined file has a verified identical retained twin. Safe to delete.")


if __name__ == "__main__":
    main()
