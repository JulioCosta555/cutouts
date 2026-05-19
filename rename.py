#!/usr/bin/env python3
"""
Strip player IDs from cutout filenames.

Walks every league/team folder under ./cutouts and renames:
    Adriano_Bertaccini_1073421.png  ->  Adriano_Bertaccini.png

If a rename would collide with an existing file (two players with the same
name on the same team — rare but possible), the ID is kept and a warning
is printed so you can resolve manually.

Usage:
    python rename_cutouts.py                     # dry run by default
    python rename_cutouts.py --apply             # actually rename
    python rename_cutouts.py --apply --root ./cutouts
    python rename_cutouts.py --apply --keep-underscores   # leave underscores intact
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

# Matches "_<digits>" right before the extension. The digits are the FotMob ID.
ID_SUFFIX_RE = re.compile(r"_(\d+)(?P<ext>\.[A-Za-z0-9]+)$")


def planned_name(filename: str, replace_underscores: bool) -> str | None:
    """Return the new filename, or None if nothing to change."""
    m = ID_SUFFIX_RE.search(filename)
    if not m:
        return None
    base = filename[: m.start()]
    ext = m.group("ext")
    if replace_underscores:
        base = base.replace("_", " ")
    return base + ext


def rename_in_dir(
    folder: Path,
    *,
    apply: bool,
    replace_underscores: bool,
) -> tuple[int, int, int]:
    """Returns (renamed, skipped_no_id, conflicts)."""
    renamed = skipped = conflicts = 0
    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        new_name = planned_name(f.name, replace_underscores)
        if new_name is None:
            skipped += 1
            continue
        if new_name == f.name:
            skipped += 1
            continue

        target = f.with_name(new_name)
        if target.exists() and target != f:
            logging.warning(
                "  CONFLICT: %s -> %s (target already exists, keeping ID)",
                f.name, new_name,
            )
            conflicts += 1
            continue

        if apply:
            f.rename(target)
        renamed += 1
        logging.debug("  %s -> %s", f.name, new_name)
    return renamed, skipped, conflicts


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Strip FotMob IDs from cutout filenames.")
    ap.add_argument("--root", type=Path, default=Path("cutouts"),
                    help="Root cutouts folder (default: ./cutouts)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually rename. Without this flag, runs as a dry preview.")
    ap.add_argument("--keep-underscores", action="store_true",
                    help="Leave underscores between names (default: replace with spaces).")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    if not args.root.is_dir():
        logging.error("Not a directory: %s", args.root)
        return 1

    if not args.apply:
        logging.info("DRY RUN — pass --apply to actually rename. (preview below)")

    replace_underscores = not args.keep_underscores

    total_renamed = total_skipped = total_conflicts = 0

    # cutouts/{league}/{team}/*.png
    for league_dir in sorted(p for p in args.root.iterdir() if p.is_dir()):
        for team_dir in sorted(p for p in league_dir.iterdir() if p.is_dir()):
            r, s, c = rename_in_dir(
                team_dir,
                apply=args.apply,
                replace_underscores=replace_underscores,
            )
            total_renamed += r
            total_skipped += s
            total_conflicts += c
            if r or c:
                logging.info(
                    "  %s/%s  renamed=%d  conflicts=%d",
                    league_dir.name, team_dir.name, r, c,
                )

    verb = "renamed" if args.apply else "would rename"
    logging.info(
        "\nDONE  %s=%d  unchanged=%d  conflicts=%d",
        verb, total_renamed, total_skipped, total_conflicts,
    )
    if not args.apply and total_renamed:
        logging.info("Re-run with --apply to commit.")

    return 0


if __name__ == "__main__":
    sys.exit(main())