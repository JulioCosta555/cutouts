#!/usr/bin/env python3
"""
Strip team IDs from logo filenames.

Walks every league folder under ./team_logos and renames:
    Real_Madrid_8633.png  ->  Real Madrid.png

Usage:
    python team_rename.py                     # dry run by default
    python team_rename.py --apply             # actually rename
    python team_rename.py --apply --keep-underscores
    python team_rename.py --apply --root ./other_logos_dir
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

# Matches "_<digits>" right before the extension.
ID_SUFFIX_RE = re.compile(r"_(\d+)(?P<ext>\.[A-Za-z0-9]+)$")


def planned_name(filename: str, replace_underscores: bool) -> str | None:
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
        if new_name is None or new_name == f.name:
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
    ap = argparse.ArgumentParser(description="Strip FotMob IDs from team logo filenames.")
    ap.add_argument("--root", type=Path, default=Path("team_logos"),
                    help="Root team_logos folder (default: ./team_logos)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually rename. Without this flag, runs as a dry preview.")
    ap.add_argument("--keep-underscores", action="store_true",
                    help="Leave underscores between words (default: replace with spaces).")
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

    # team_logos/{league}/*.png  (no nested team subfolder)
    for league_dir in sorted(p for p in args.root.iterdir() if p.is_dir()):
        r, s, c = rename_in_dir(
            league_dir,
            apply=args.apply,
            replace_underscores=replace_underscores,
        )
        total_renamed += r
        total_skipped += s
        total_conflicts += c
        if r or c:
            logging.info(
                "  %s  renamed=%d  conflicts=%d",
                league_dir.name, r, c,
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