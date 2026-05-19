#!/usr/bin/env python3
"""
FotMob League Logo Downloader
=============================
Downloads each league's logo from FotMob's image CDN.

Output:
    league_logos/
        premier-league_47.png
        laliga_87.png
        ...

Usage:
    pip install requests
    python download_league_logos.py
    python download_league_logos.py --leagues premier-league laliga
    python download_league_logos.py --out ./logos
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


LEAGUES: dict[str, int] = {
    "premier-league":  47,
    "laliga":          87,
    "ligue-1":         53,
    "serie-a":         55,
    "first-division":  40,
    "liga-portugal":   61,
    "bundesliga":      54,
    "eredivisie":      57,
}

LOGO_URL = "https://images.fotmob.com/image_resources/logo/leaguelogo/{league_id}.png"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "image/png,image/*,*/*;q=0.8",
        "Referer": "https://www.fotmob.com/",
    })
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    return s


def download_logo(session: requests.Session, slug: str, league_id: int, out_dir: Path) -> str:
    """Returns 'ok', 'skip', or 'miss'."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fpath = out_dir / f"{slug}_{league_id}.png"

    if fpath.exists() and fpath.stat().st_size > 0:
        return "skip"

    url = LOGO_URL.format(league_id=league_id)
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
    except requests.RequestException as e:
        logging.warning("Network error for %s: %s", slug, e)
        return "miss"

    if r.status_code in (403, 404):
        logging.warning("No logo on CDN for %s (id=%d) — HTTP %s", slug, league_id, r.status_code)
        return "miss"
    if r.status_code != 200:
        logging.warning("HTTP %s for %s (id=%d)", r.status_code, slug, league_id)
        return "miss"
    if "image" not in r.headers.get("Content-Type", ""):
        logging.warning("Non-image response for %s (Content-Type=%r)",
                        slug, r.headers.get("Content-Type"))
        return "miss"

    with open(fpath, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)

    if fpath.stat().st_size == 0:
        fpath.unlink(missing_ok=True)
        return "miss"

    return "ok"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Download FotMob league logo images.")
    ap.add_argument(
        "--leagues",
        nargs="+",
        choices=list(LEAGUES.keys()),
        default=list(LEAGUES.keys()),
    )
    ap.add_argument("--out", type=Path, default=Path("league_logos"))
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    session = make_session()
    args.out.mkdir(parents=True, exist_ok=True)

    counts = {"ok": 0, "skip": 0, "miss": 0}
    for slug in args.leagues:
        if slug not in LEAGUES:
            logging.warning("Unknown league %r — skipping", slug)
            continue
        league_id = LEAGUES[slug]
        result = download_logo(session, slug, league_id, args.out)
        counts[result] += 1
        marker = {"ok": "↓", "skip": "✓", "miss": "✗"}[result]
        logging.info("  %s  %-18s id=%d", marker, slug, league_id)

    logging.info(
        "DONE  downloaded=%d  already_present=%d  failed=%d",
        counts["ok"], counts["skip"], counts["miss"],
    )
    return 0 if counts["miss"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())