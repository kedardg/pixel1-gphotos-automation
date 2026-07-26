# Duplicate finder / cleaner

Two-step, non-destructive de-duplication for a photo/video folder before
uploading. Finds **byte-identical** copies only (hash-confirmed), so different
formats of the same shot (HEIC vs JPEG) and different edits are never touched.

## Method

`find_dupes.py` layers the checks the way you would by hand — **name → size →
hash** — but only an identical hash marks a file for removal:

1. Walk the tree, skip junk (`.DS_Store`, `._*`).
2. Group by exact byte size. A unique size can't be a duplicate, so it's never
   read — only same-size collisions get hashed (keeps the run cheap).
3. Full SHA-256 every file in a size-collision bucket.
4. Group by hash. Any group with >1 file = byte-identical duplicates. Each group
   records the filename-stem overlap so the name relationship is visible.

## Files

- `find_dupes.py ROOT report.json` — read-only scan → JSON report + summary.
- `clean_dupes.py report.json [--apply]` — keeps one copy per group and **moves**
  the rest to a quarantine folder (`ROOT/_dup_quarantine/`, relative paths
  preserved). Nothing is hard-deleted; you empty the quarantine yourself once
  satisfied. Re-hashes each file before moving unless `--no-verify`.
- `cross_dupes.py TARGET --ref REF1 [--ref REF2 ...]` — **cross-folder** pass:
  finds files in TARGET that are byte-identical to something already in a
  reference folder (== already in your archive / already uploaded) and moves
  those out of TARGET into `TARGET/_xref_quarantine/`, so a new batch doesn't
  re-upload what you already have. References are read-only; only TARGET is
  touched. Same size→hash economy. `--report r.json` saves the match list;
  `--from-report r.json --apply` moves from a saved list without re-hashing
  (add `--no-verify` to skip the per-file `cmp` recheck when the tree is
  unchanged since the scan).

- `verify_quarantine.py --root ROOT [--intra r.json] [--xref r.json]` — final
  gate before you delete a quarantine folder. Independently `cmp`s every
  quarantined file against a **retained** twin (archive copy for xref dupes; the
  kept group copy for intra dupes, resolved even if it was later moved or
  redated). If a twin differs only in metadata (a keeper that was redated after
  quarantine), it falls back to an ffmpeg essence hash and reports OK-STREAM.
  Exits non-zero if anything is a real DIFFER/MISSING.

## Keep rule (which copy survives)

The copy that sorts first is kept:

1. GoPro clips (`GX/GL/GH0125*`) prefer to live under `100GOPRO/`, so a copy
   there beats a loose copy in the root dump.
2. A name **without** a `" (N)"` copy suffix beats the `" (N)"` copy.
3. Shorter path, then lexicographic — deterministic tie-break.

## Workflow

```bash
# 1. scan (read-only)
.venv/bin/python tools/dedup/find_dupes.py "/path/to/folder" tools/dedup/_local/dupes.json

# 2. preview what would move (dry-run is the default)
.venv/bin/python tools/dedup/clean_dupes.py tools/dedup/_local/dupes.json

# 3. quarantine the duplicates
.venv/bin/python tools/dedup/clean_dupes.py tools/dedup/_local/dupes.json --apply

# 4. eyeball ROOT/_dup_quarantine, then delete it yourself to reclaim space
```

Cross-folder, before uploading a new batch (drop anything already archived):

```bash
# scan target against your archive folders (read-only report)
.venv/bin/python tools/dedup/cross_dupes.py "/path/to/new batch" \
  --ref "/path/to/archive A" --ref "/path/to/archive B" \
  --report tools/dedup/_local/xref_dupes.json

# move the already-archived copies out of the new batch
.venv/bin/python tools/dedup/cross_dupes.py "/path/to/new batch" \
  --ref "/path/to/archive A" --ref "/path/to/archive B" \
  --from-report tools/dedup/_local/xref_dupes.json --apply
```

Reports live in a gitignored `_local/` folder (they contain personal file paths).

## Requires

Python 3 standard library only (`hashlib`, `os`). No exiftool needed.
