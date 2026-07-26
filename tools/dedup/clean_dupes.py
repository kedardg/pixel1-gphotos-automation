#!/usr/bin/env python3
"""Quarantine the duplicate copies found by find_dupes.py.

For each duplicate group it keeps exactly ONE copy and MOVES the rest into a
quarantine folder (default: ROOT/_dup_quarantine/), preserving their relative
path. Nothing is hard-deleted -- once you've eyeballed the quarantine folder,
delete it yourself (Finder Trash, or `rm -rf`) to reclaim the space. This keeps
the destructive step in your hands and fully reversible until then.

Keep priority within a group (the copy that sorts FIRST is kept):
  1. GoPro clips (GX/GL/GH0125*) prefer to live under 100GOPRO/, so a copy
     there outranks a loose copy in the root dump.
  2. A name WITHOUT a " (N)" copy suffix outranks the " (N)" copy.
  3. Shorter relative path, then lexicographic order -- deterministic tie-break.

By default every file is re-hashed against the report before being moved, so a
tree that changed since the scan can't cause a wrong deletion (use --no-verify
to skip for speed).

Usage:
  clean_dupes.py report.json                 # dry-run: print keep/move plan
  clean_dupes.py report.json --apply         # perform the moves
  clean_dupes.py report.json --apply --quarantine /some/dir
"""
import os, json, argparse, re, hashlib, shutil

COPY = re.compile(r"^(.*) \(\d+\)$")


def is_gopro(base):
    return base.upper().startswith(("GX0125", "GL0125", "GH0125", "GX0126", "GL0126"))


def keep_key(rel):
    """Lower sorts first == kept."""
    base = os.path.basename(rel)
    stem = os.path.splitext(base)[0]
    under_gopro = rel.startswith("100GOPRO/") or "/100GOPRO/" in ("/" + rel)
    gopro_pref = 0 if (is_gopro(base) and under_gopro) else 1
    copy_pref = 1 if COPY.match(stem) else 0
    return (gopro_pref, copy_pref, len(rel), rel)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f}{u}"
        n /= 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--root", help="override report root")
    ap.add_argument("--quarantine", help="quarantine dir (default ROOT/_dup_quarantine)")
    ap.add_argument("--apply", action="store_true", help="actually move (default dry-run)")
    ap.add_argument("--no-verify", action="store_true", help="skip re-hash check before moving")
    args = ap.parse_args()

    rep = json.load(open(args.report))
    root = os.path.abspath(args.root or rep["root"])
    quar = os.path.abspath(args.quarantine or os.path.join(root, "_dup_quarantine"))

    moved = kept = skipped = 0
    freed = 0
    for g in rep["groups"]:
        rels = sorted(g["paths"], key=keep_key)
        keep, drop = rels[0], rels[1:]
        kept += 1
        print(f"KEEP  {keep}")
        for rel in drop:
            src = os.path.join(root, rel)
            if not os.path.exists(src):
                print(f"  --   already gone: {rel}")
                skipped += 1
                continue
            if not args.no_verify and args.apply:
                if sha256(src) != g["hash"]:
                    print(f"  !!   HASH CHANGED, skipping: {rel}")
                    skipped += 1
                    continue
            dst = os.path.join(quar, rel)
            print(f"  ->   {rel}")
            if args.apply:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            moved += 1
            freed += g["size"]

    print()
    print(f"groups: {len(rep['groups'])} | keep 1 each: {kept}")
    print(f"{'moved' if args.apply else 'would move'}: {moved} copies -> {quar}")
    print(f"space {'freed to quarantine' if args.apply else 'to reclaim'}: {human(freed)}")
    if skipped:
        print(f"skipped (missing/changed): {skipped}")
    if not args.apply:
        print("\n[dry-run] nothing moved. re-run with --apply to quarantine.")
    else:
        print(f"\nReview {quar}, then delete it yourself to reclaim the space.")


if __name__ == "__main__":
    main()
