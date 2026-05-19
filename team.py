#!/usr/bin/env python3
"""
FotMob Team Logo Downloader
===========================
Walks each selected league, finds every team's FotMob ID, and downloads the
team logo from FotMob's image CDN.

Output:
    team_logos/
        premier-league/
            Arsenal_9825.png
            Liverpool_8650.png
            ...
        laliga/
            Real_Madrid_8633.png
            ...

The first run scrapes FotMob to discover team IDs (same __NEXT_DATA__ trick
as download_squads.py) and caches the league→teams mapping as JSON. Later
runs read the cache, so they're instant.

Usage:
    pip install requests
    python team.py
    python team.py --leagues premier-league laliga
    python team.py --refresh                 # re-scrape, ignore cache
    python team.py --workers 16 --out ./logos
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LEAGUES: dict[str, tuple[int, str]] = {
    "premier-league":  (47, "premier-league"),
    "laliga":          (87, "laliga"),
    "ligue-1":         (53, "ligue-1"),
    "serie-a":         (55, "serie"),
    "first-division":  (40, "first-division-a"),
    "liga-portugal":   (61, "liga-portugal"),
    "bundesliga":      (54, "bundesliga"),
    "eredivisie":      (57, "eredivisie"),
}

FOTMOB_BASE = "https://www.fotmob.com"
LEAGUE_PAGE = "/leagues/{league_id}/overview/{slug}"

LOGO_URL = "https://images.fotmob.com/image_resources/logo/teamlogo/{team_id}.png"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_PAGE_LOADS = 0.5

CACHE_FILE = Path(".team_ids_cache.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class Team:
    id: int
    name: str
    league_slug: str


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.fotmob.com/",
    })
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    return s


def safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name or "unnamed"


# ---------------------------------------------------------------------------
# __NEXT_DATA__ extraction (same approach as download_squads.py)
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>',
    re.DOTALL,
)


def fetch_next_data(session: requests.Session, path: str) -> dict | None:
    url = FOTMOB_BASE + path
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        return None
    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        return None
    try:
        return json.loads(m.group("json"))
    except json.JSONDecodeError:
        return None


def _looks_like_team(node: dict) -> bool:
    if not isinstance(node, dict):
        return False
    if not isinstance(node.get("id"), (int, str)):
        return False
    if not isinstance(node.get("name"), str) or not node["name"]:
        return False
    has_team_marker = any(k in node for k in ("shortName", "logoUrl", "logo", "pageUrl"))
    has_player_marker = any(k in node for k in (
        "playerId", "shirtNumber", "positionId", "ccode",
    ))
    return has_team_marker and not has_player_marker


def _walk(node, predicate, accumulator: dict[int, str]) -> None:
    if isinstance(node, dict):
        if predicate(node):
            try:
                accumulator.setdefault(int(node["id"]), node["name"])
            except (TypeError, ValueError):
                pass
        for v in node.values():
            _walk(v, predicate, accumulator)
    elif isinstance(node, list):
        for v in node:
            _walk(v, predicate, accumulator)


def extract_team_ids(next_data: dict) -> list[tuple[int, str]]:
    teams: dict[int, str] = {}
    _walk(next_data, _looks_like_team, teams)
    league_ids = {lid for (lid, _slug) in LEAGUES.values()}
    league_display_names = {
        "Premier League", "LaLiga", "Ligue 1", "Serie A",
        "First Division A", "Liga Portugal", "Bundesliga", "Eredivisie",
    }
    teams = {
        tid: name for tid, name in teams.items()
        if tid not in league_ids and name not in league_display_names
    }
    return sorted(teams.items(), key=lambda x: x[1].lower())


# ---------------------------------------------------------------------------
# Team discovery (with caching)
# ---------------------------------------------------------------------------

def discover_teams(
    session: requests.Session,
    league_slugs: list[str],
    cache_path: Path,
    refresh: bool,
) -> list[Team]:
    cache: dict[str, list[dict]] = {}
    if cache_path.exists() and not refresh:
        try:
            cache = json.loads(cache_path.read_text())
            logging.info("Loaded team-id cache (%s)", cache_path)
        except json.JSONDecodeError:
            logging.warning("Cache corrupt — re-scraping.")

    all_teams: list[Team] = []
    cache_dirty = False

    for slug in league_slugs:
        if slug in cache:
            for entry in cache[slug]:
                all_teams.append(Team(id=entry["id"], name=entry["name"], league_slug=slug))
            logging.info("  %s  %d teams (cached)", slug, len(cache[slug]))
            continue

        league_id, page_slug = LEAGUES[slug]
        nd = fetch_next_data(session, LEAGUE_PAGE.format(league_id=league_id, slug=page_slug))
        if nd is None:
            logging.error("  %s  could not load league page", slug)
            continue
        teams = extract_team_ids(nd)
        cache[slug] = [{"id": tid, "name": name} for tid, name in teams]
        cache_dirty = True
        for tid, name in teams:
            all_teams.append(Team(id=tid, name=name, league_slug=slug))
        logging.info("  %s  %d teams (scraped)", slug, len(teams))
        time.sleep(SLEEP_BETWEEN_PAGE_LOADS)

    if cache_dirty:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
        logging.debug("Wrote team-id cache → %s", cache_path)

    return all_teams


# ---------------------------------------------------------------------------
# Logo download
# ---------------------------------------------------------------------------

def download_logo(session: requests.Session, team: Team, out_root: Path) -> str:
    out_dir = out_root / team.league_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{safe_filename(team.name)}_{team.id}.png"
    fpath = out_dir / fname

    if fpath.exists() and fpath.stat().st_size > 0:
        return "skip"

    url = LOGO_URL.format(team_id=team.id)
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
    except requests.RequestException as e:
        logging.warning("Network error for %s (%d): %s", team.name, team.id, e)
        return "miss"

    if r.status_code in (403, 404):
        return "miss"
    if r.status_code != 200:
        logging.warning("HTTP %s for %s (%d)", r.status_code, team.name, team.id)
        return "miss"
    if "image" not in r.headers.get("Content-Type", ""):
        return "miss"

    with open(fpath, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if chunk:
                f.write(chunk)

    if fpath.stat().st_size == 0:
        fpath.unlink(missing_ok=True)
        return "miss"

    return "ok"


def download_all(teams: list[Team], out_root: Path, workers: int) -> dict[str, int]:
    session = make_session()
    counts = {"ok": 0, "skip": 0, "miss": 0}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(download_logo, session, t, out_root): t for t in teams}
        for i, fut in enumerate(as_completed(futures), 1):
            t = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logging.warning("Crashed for %s: %s", t.name, e)
                result = "miss"
            counts[result] += 1
            if i % 25 == 0 or i == len(futures):
                logging.info(
                    "  progress %d/%d  ok=%d skip=%d miss=%d",
                    i, len(futures), counts["ok"], counts["skip"], counts["miss"],
                )
    return counts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Download FotMob team logo images.")
    ap.add_argument(
        "--leagues",
        nargs="+",
        choices=list(LEAGUES.keys()),
        default=list(LEAGUES.keys()),
    )
    ap.add_argument("--out", type=Path, default=Path("team_logos"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore the team-id cache and re-scrape FotMob.")
    ap.add_argument("--cache", type=Path, default=CACHE_FILE,
                    help=f"Path to team-id cache file (default: {CACHE_FILE})")
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

    logging.info("Discovering teams in %d league(s)…", len(args.leagues))
    teams = discover_teams(session, args.leagues, args.cache, args.refresh)
    logging.info("Total teams: %d", len(teams))

    if not teams:
        logging.error("No teams found — aborting.")
        return 1

    logging.info("Downloading logos with %d workers → %s", args.workers, args.out)
    counts = download_all(teams, args.out, args.workers)
    logging.info(
        "DONE  downloaded=%d  already_present=%d  no_logo=%d  total=%d",
        counts["ok"], counts["skip"], counts["miss"], sum(counts.values()),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())