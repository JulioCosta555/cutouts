#!/usr/bin/env python3
"""
FotMob Squad Cutout Downloader
==============================
Walks each selected league → its current-season teams → their squads, then
downloads every player's transparent cutout PNG from FotMob's image CDN.

Strategy
--------
FotMob's `/api/*` endpoints now require a signed `x-mas` header, which makes
direct API calls fragile. This script instead reads the same data from the
`__NEXT_DATA__` JSON blob that FotMob's HTML pages embed for hydration —
that route is unauthenticated and stable across signature rotations.

Output
------
    cutouts/
        premier-league/
            Arsenal/
                Bukayo_Saka_558326.png
                ...

Usage
-----
    pip install requests
    python download_squads.py
    python download_squads.py --leagues premier-league laliga
    python download_squads.py --workers 16 --out ./images --manifest players.json
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
from typing import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# slug → (league_id, page_slug). page_slug is what fotmob uses in its URLs.
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
TEAM_PAGE   = "/teams/{team_id}/squad"

CUTOUT_URL = "https://images.fotmob.com/image_resources/playerimages/{player_id}.png"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_PAGE_LOADS = 0.5  # be polite — these are full HTML pages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class Player:
    id: int
    name: str
    team_name: str
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
# __NEXT_DATA__ extraction
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>',
    re.DOTALL,
)


def fetch_next_data(session: requests.Session, path: str) -> dict | None:
    """GET an HTML page and return its parsed __NEXT_DATA__ JSON."""
    url = FOTMOB_BASE + path
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        logging.debug("HTTP %s for %s", r.status_code, url)
        return None
    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        logging.debug("No __NEXT_DATA__ in %s", url)
        return None
    try:
        return json.loads(m.group("json"))
    except json.JSONDecodeError as e:
        logging.debug("JSON parse failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Team / squad parsers — robust to FotMob's frequent shape changes
# ---------------------------------------------------------------------------

def _looks_like_team(node: dict) -> bool:
    """Heuristic: is this dict a team entry?"""
    if not isinstance(node, dict):
        return False
    if not isinstance(node.get("id"), (int, str)):
        return False
    if not isinstance(node.get("name"), str) or not node["name"]:
        return False
    # Teams have a logo or a short name; players don't.
    has_team_marker = any(k in node for k in (
        "shortName", "logoUrl", "logo", "pageUrl",
    ))
    # And teams never carry per-player markers.
    has_player_marker = any(k in node for k in (
        "playerId", "shirtNumber", "positionId", "ccode",
    ))
    return has_team_marker and not has_player_marker


def _looks_like_player(node: dict) -> bool:
    if not isinstance(node, dict):
        return False
    if not isinstance(node.get("id"), (int, str)):
        return False
    name = node.get("name") or node.get("playerName")
    if not isinstance(name, str) or not name:
        return False
    has_player_marker = any(k in node for k in (
        "shirtNumber", "positionId", "ccode", "role",
        "positionIdsDesc", "performance", "playerId",
    ))
    has_team_marker = any(k in node for k in (
        "shortName", "logoUrl", "logo",
    ))
    return has_player_marker and not has_team_marker


def _walk(node, predicate, accumulator: dict[int, str], name_key_fallback="name") -> None:
    if isinstance(node, dict):
        if predicate(node):
            try:
                pid = int(node["id"])
                pname = node.get("name") or node.get(name_key_fallback)
                if pname:
                    accumulator.setdefault(pid, pname)
            except (TypeError, ValueError):
                pass
        for v in node.values():
            _walk(v, predicate, accumulator, name_key_fallback)
    elif isinstance(node, list):
        for v in node:
            _walk(v, predicate, accumulator, name_key_fallback)


def extract_team_ids(next_data: dict) -> list[tuple[int, str]]:
    """Find all team (id, name) pairs in a league page's __NEXT_DATA__."""
    teams: dict[int, str] = {}
    _walk(next_data, _looks_like_team, teams)
    return sorted(teams.items(), key=lambda x: x[1].lower())


def extract_squad(next_data: dict) -> list[tuple[int, str]]:
    """Find all player (id, name) pairs in a team page's __NEXT_DATA__."""
    players: dict[int, str] = {}
    _walk(next_data, _looks_like_player, players, name_key_fallback="playerName")
    return sorted(players.items(), key=lambda x: x[1].lower())


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------

def download_cutout(session: requests.Session, player: Player, out_dir: Path) -> str:
    """Returns 'ok', 'skip', or 'miss'."""
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{safe_filename(player.name)}_{player.id}.png"
    fpath = out_dir / fname

    if fpath.exists() and fpath.stat().st_size > 0:
        return "skip"

    url = CUTOUT_URL.format(player_id=player.id)
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
    except requests.RequestException as e:
        logging.warning("Network error for %s (%d): %s", player.name, player.id, e)
        return "miss"

    if r.status_code == 404:
        return "miss"
    if r.status_code != 200:
        logging.warning("HTTP %s for %s (%d)", r.status_code, player.name, player.id)
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


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def collect_all_players(
    session: requests.Session,
    league_slugs: Iterable[str],
) -> list[Player]:
    all_players: list[Player] = []

    for slug in league_slugs:
        if slug not in LEAGUES:
            logging.warning("Unknown league %r — skipping", slug)
            continue

        league_id, page_slug = LEAGUES[slug]
        logging.info("=== %s (id=%d) ===", slug, league_id)

        league_path = LEAGUE_PAGE.format(league_id=league_id, slug=page_slug)
        nd = fetch_next_data(session, league_path)
        if nd is None:
            logging.error("  could not load league page %s", league_path)
            continue

        teams = extract_team_ids(nd)
        if not teams:
            logging.error("  no teams found on league page (parser may need update)")
            continue
        logging.info("  found %d teams", len(teams))

        for team_id, team_name in teams:
            time.sleep(SLEEP_BETWEEN_PAGE_LOADS)
            team_path = TEAM_PAGE.format(team_id=team_id)
            tnd = fetch_next_data(session, team_path)
            if tnd is None:
                logging.warning("    team %s (%d) page failed", team_name, team_id)
                continue

            squad = extract_squad(tnd)
            for pid, pname in squad:
                all_players.append(Player(
                    id=pid, name=pname,
                    team_name=team_name, league_slug=slug,
                ))
            logging.info("    %-30s %3d players", team_name[:30], len(squad))

    # de-dup on (league, team, player_id)
    seen: set[tuple[str, str, int]] = set()
    unique: list[Player] = []
    for p in all_players:
        key = (p.league_slug, p.team_name, p.id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def download_all(players: list[Player], out_root: Path, workers: int) -> dict[str, int]:
    session = make_session()
    counts = {"ok": 0, "skip": 0, "miss": 0}

    def task(p: Player) -> str:
        team_dir = out_root / p.league_slug / safe_filename(p.team_name)
        return download_cutout(session, p, team_dir)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(task, p): p for p in players}
        for i, fut in enumerate(as_completed(futures), 1):
            p = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logging.warning("Crashed for %s: %s", p.name, e)
                result = "miss"
            counts[result] += 1
            if i % 50 == 0 or i == len(futures):
                logging.info(
                    "  progress %d/%d  ok=%d skip=%d miss=%d",
                    i, len(futures), counts["ok"], counts["skip"], counts["miss"],
                )
    return counts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Download FotMob player cutout photos.")
    ap.add_argument(
        "--leagues",
        nargs="+",
        choices=list(LEAGUES.keys()),
        default=list(LEAGUES.keys()),
    )
    ap.add_argument("--out", type=Path, default=Path("cutouts"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--manifest", type=Path, default=None,
                    help="Optional path to write a JSON manifest of all players.")
    ap.add_argument("--debug-dump", type=Path, default=None,
                    help="Dump the Premier League __NEXT_DATA__ payload here for inspection.")
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

    if args.debug_dump:
        league_id, slug = LEAGUES["premier-league"]
        nd = fetch_next_data(session, LEAGUE_PAGE.format(league_id=league_id, slug=slug))
        if nd is not None:
            args.debug_dump.write_text(json.dumps(nd, indent=2, ensure_ascii=False))
            logging.info("Wrote debug dump to %s", args.debug_dump)
        else:
            logging.error("Could not fetch debug page.")

    logging.info("Collecting squads from %d league(s)…", len(args.leagues))
    players = collect_all_players(session, args.leagues)
    logging.info("Total unique player records: %d", len(players))

    if args.manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps([p.__dict__ for p in players], ensure_ascii=False, indent=2)
        )
        logging.info("Wrote manifest → %s", args.manifest)

    if not players:
        logging.error("No players found — aborting.")
        return 1

    logging.info("Downloading cutouts with %d workers → %s", args.workers, args.out)
    counts = download_all(players, args.out, args.workers)
    logging.info(
        "DONE  downloaded=%d  already_present=%d  no_cutout=%d  total=%d",
        counts["ok"], counts["skip"], counts["miss"], sum(counts.values()),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())