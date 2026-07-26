#!/usr/bin/env python3
"""Find files in TARGET that already exist (byte-identical) in any REFERENCE
folder, and quarantine those copies OUT of TARGET.

Use before uploading a new batch to avoid re-uploading anything already in your
archive (== already uploaded) and wasting bandwidth. Only TARGET is ever
touched; reference folders are read-only.

Method (cheap by construction):
  1. Walk TARGET (skip junk + its own quarantine dirs); index by size.
  2. Walk each REFERENCE; keep only files whose size also occurs in TARGET.
  3. For each size present in both, hash the reference files into a set, then
     hash each TARGET file of that size and check membership. A hit == the
     TARGET file is byte-identical to something already in a reference.
Files whose size never appears in a reference are never hashed.

Matches are MOVED to TARGET/_xref_quarantine/ (relative path preserved), never
hard-deleted. Review, then delete it yourself.

Usage:
  cross_dupes.py TARGET --ref REF1 [--ref REF2 ...] [--apply]
                 [--quarantine DIR] [--report report.json]
"""
import os, sys, json, argparse, hashlib, collections


def is_junk(name):
    return name == ".DS_Store" or name.startswith("._")


QUAR_DIRS = ("_dup_quarantine", "_xref_quarantine")


def walk_files(root, skip_quar=False):
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_quar:
            dirnames[:] = [d for d in dirnames if d not in QUAR_DIRS]
        for n in filenames:
            if is_junk(n):
                continue
            yield os.path.join(dirpath, n)


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
    ap.add_argument("target")
    ap.add_argument("--ref", action="append", required=True, help="reference folder (repeatable)")
    ap.add_argument("--quarantine")
    ap.add_argument("--report")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--from-report", help="skip detection; move matches listed in this report JSON")
    ap.add_argument("--no-verify", action="store_true", help="with --from-report: skip per-file cmp recheck")
    args = ap.parse_args()

    target = os.path.abspath(args.target)
    quar = os.path.abspath(args.quarantine or os.path.join(target, "_xref_quarantine"))

    # fast path: apply an already-computed report without re-hashing
    if args.from_report:
        import shutil, subprocess
        rep = json.load(open(args.from_report))
        matches = rep["matches"]
        moved = skipped = 0
        for m in matches:
            src = os.path.join(target, m["target"])
            if not os.path.exists(src):
                skipped += 1
                continue
            if not args.no_verify:
                if subprocess.run(["cmp", "-s", src, m["ref_match"]]).returncode != 0:
                    print(f"  !! not identical anymore, skipping: {m['target']}")
                    skipped += 1
                    continue
            if args.apply:
                dst = os.path.join(quar, m["target"])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            moved += 1
        print(f"{'moved' if args.apply else 'would move'} {moved} files -> {quar}"
              + (f" | skipped {skipped}" if skipped else ""))
        if not args.apply:
            print("[dry-run] add --apply to move.")
        return

    # 1. index TARGET by size
    target_by_size = collections.defaultdict(list)
    t_total = 0
    for p in walk_files(target, skip_quar=True):
        try:
            target_by_size[os.path.getsize(p)].append(p)
        except OSError:
            continue
        t_total += 1
    sys.stderr.write(f"target: {t_total} files, {len(target_by_size)} distinct sizes\n")

    # 2. reference files whose size occurs in target
    ref_by_size = collections.defaultdict(list)
    r_total = r_kept = 0
    for ref in args.ref:
        for p in walk_files(os.path.abspath(ref), skip_quar=False):
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            r_total += 1
            if sz in target_by_size:
                ref_by_size[sz].append(p)
                r_kept += 1
    sys.stderr.write(f"refs: {r_total} files scanned, {r_kept} size-candidates\n")

    # 3. hash within shared sizes only
    shared = sorted((s for s in target_by_size if s in ref_by_size),
                    key=lambda s: -s * len(target_by_size[s]))
    matches = []            # {target_rel, size, ref_match}
    done = 0
    for sz in shared:
        ref_hash = {}
        for rp in ref_by_size[sz]:
            try:
                ref_hash.setdefault(sha256(rp), rp)
            except OSError:
                pass
        for tp in target_by_size[sz]:
            try:
                h = sha256(tp)
            except OSError:
                continue
            if h in ref_hash:
                matches.append({
                    "target": os.path.relpath(tp, target),
                    "size": sz,
                    "ref_match": ref_hash[h],
                })
            done += 1
            if done % 50 == 0:
                sys.stderr.write(f"  checked {done} target candidates\n"); sys.stderr.flush()

    matches.sort(key=lambda m: -m["size"])
    freed = sum(m["size"] for m in matches)

    if args.report:
        with open(args.report, "w") as f:
            json.dump({"target": target, "refs": [os.path.abspath(r) for r in args.ref],
                       "matches": matches, "reclaimable_bytes": freed}, f, indent=2)

    print(f"target files            : {t_total}")
    print(f"already-in-archive dupes: {len(matches)}")
    print(f"reclaimable / not-upload: {human(freed)}")
    ext = collections.Counter(os.path.splitext(m["target"])[1].lower() for m in matches)
    if ext:
        print("by type: " + ", ".join(f"{v}{k}" for k, v in ext.most_common()))
    print()
    for m in matches[:40]:
        print(f"  [{human(m['size'])}] {m['target']}")
        print(f"        == {m['ref_match']}")
    if len(matches) > 40:
        print(f"  ... and {len(matches) - 40} more (see --report)")

    if not args.apply:
        print("\n[dry-run] nothing moved. re-run with --apply to quarantine matches out of target.")
        return

    import shutil
    moved = 0
    for m in matches:
        src = os.path.join(target, m["target"])
        if not os.path.exists(src):
            continue
        dst = os.path.join(quar, m["target"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        moved += 1
    print(f"\nmoved {moved} already-archived files -> {quar}")
    print("Review it, then delete it yourself.")


if __name__ == "__main__":
    main()
