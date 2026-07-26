#!/usr/bin/env python3
"""Find byte-identical duplicate files under a folder tree.

Layered the way you'd verify a duplicate by hand: name -> size -> hash, but the
only deletion-worthy signal is an identical hash.

  1. Walk the tree, skip junk (.DS_Store, ._AppleDouble).
  2. Group by exact byte size. A size that only one file has cannot be a
     duplicate, so it is never hashed (this is what keeps the run cheap -- only
     same-size collisions are read).
  3. Full SHA-256 every file in a size-collision bucket.
  4. Group by (hash, size). Any group with >1 file is a set of byte-identical
     duplicates. Each group is annotated with filename-stem overlap so the
     name-substring relationship is visible in the report.

Read-only. Writes a JSON report (consumed by clean_dupes.py) and prints a
human summary, largest-wasted-space first. Deletes nothing.

Usage:
  find_dupes.py ROOT_DIR report.json
"""
import os, sys, json, hashlib, collections


def is_junk(name):
    return name == ".DS_Store" or name.startswith("._")


# this toolchain's own quarantine folders -- never scan them back in
QUAR_DIRS = ("_dup_quarantine", "_xref_quarantine")


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
    if len(sys.argv) != 3:
        sys.exit("usage: find_dupes.py ROOT_DIR report.json")
    root, out = sys.argv[1], sys.argv[2]

    # 1. collect sizes
    by_size = collections.defaultdict(list)
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in QUAR_DIRS]
        for n in filenames:
            if is_junk(n):
                continue
            p = os.path.join(dirpath, n)
            try:
                by_size[os.path.getsize(p)].append(p)
            except OSError:
                continue
            total += 1

    # 2. hash only within size collisions, biggest potential waste first
    buckets = [(sz, ps) for sz, ps in by_size.items() if len(ps) > 1 and sz > 0]
    buckets.sort(key=lambda x: -x[0] * len(x[1]))
    to_hash = sum(len(ps) for _, ps in buckets)
    sys.stderr.write(f"{total} files; {to_hash} in {len(buckets)} same-size buckets to hash\n")
    sys.stderr.flush()

    by_hash = collections.defaultdict(list)
    done = 0
    for sz, ps in buckets:
        for p in ps:
            try:
                by_hash[(sha256(p), sz)].append(p)
            except OSError as e:
                sys.stderr.write(f"skip {p}: {e}\n")
            done += 1
            if done % 50 == 0:
                sys.stderr.write(f"  hashed {done}/{to_hash}\n"); sys.stderr.flush()

    # 3. duplicate groups
    groups = []
    for (h, sz), ps in by_hash.items():
        if len(ps) < 2:
            continue
        ps = sorted(ps)
        groups.append({
            "hash": h,
            "size": sz,
            "count": len(ps),
            "extra_copies": len(ps) - 1,
            "same_basename": len({os.path.basename(p) for p in ps}) == 1,
            "stems": sorted({os.path.splitext(os.path.basename(p))[0] for p in ps}),
            "paths": [os.path.relpath(p, root) for p in ps],
        })
    groups.sort(key=lambda g: -g["size"] * g["extra_copies"])

    reclaim = sum(g["size"] * g["extra_copies"] for g in groups)
    with open(out, "w") as f:
        json.dump({
            "root": os.path.abspath(root),
            "total_files": total,
            "duplicate_groups": len(groups),
            "removable_files": sum(g["extra_copies"] for g in groups),
            "reclaimable_bytes": reclaim,
            "groups": groups,
        }, f, indent=2)

    print(f"total files scanned : {total}")
    print(f"duplicate groups    : {len(groups)}")
    print(f"removable copies    : {sum(g['extra_copies'] for g in groups)}")
    print(f"reclaimable space   : {human(reclaim)}")
    print(f"report written to   : {out}")


if __name__ == "__main__":
    main()
