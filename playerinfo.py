#!/usr/bin/env python3
"""
FotMob League Players → CSV
===========================
Walks each selected league → its current-season teams → their squads, then
collects every player's basic profile into a single CSV.

Strategy
--------
FotMob's `/api/*` endpoints now require a signed `x-mas` header and 404
anonymous clients. This script reads the same data from the `__NEXT_DATA__`
JSON blob embedded in FotMob's HTML pages — that route is unauthenticated
and stable across signature rotations.

Most wanted fields (name, team, country, role) come straight from the
team-page squad blob. Age & height live only on the per-player page, so
the script fetches each player page as well — disable with --skip-player-page
for a much faster run that drops only those two columns.

Output
------
    fotmob_league_players.csv

Usage
-----
    pip install requests pandas
    python leagueplayers.py
    python leagueplayers.py --leagues premier-league laliga
    python leagueplayers.py --skip-player-page          # much faster
    python leagueplayers.py --workers 16 --out custom.csv
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import pandas as pd
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
PLAYER_PAGE = "/players/{player_id}"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_PAGE_LOADS = 0.3


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PlayerRow:
    league: str
    id: int
    name: str
    teamId: int | None = None
    teamName: str | None = None
    role: str | None = None
    country: str | None = None
    age: str | None = None
    height: str | None = None
    shirtNumber: str | None = None


# ---------------------------------------------------------------------------
# Session / HTML fetching
# ---------------------------------------------------------------------------

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
    s.mount("http://", adapter)
    return s


_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>',
    re.DOTALL,
)


def fetch_next_data(session: requests.Session, path: str) -> dict | None:
    """GET an HTML page and return its parsed __NEXT_DATA__ JSON."""
    url = FOTMOB_BASE + path
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        logging.debug("Network error for %s: %s", url, e)
        return None
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
# __NEXT_DATA__ walkers
# ---------------------------------------------------------------------------

def _looks_like_team(node: dict) -> bool:
    if not isinstance(node, dict):
        return False
    if not isinstance(node.get("id"), (int, str)):
        return False
    if not isinstance(node.get("name"), str) or not node["name"]:
        return False
    has_team_marker = any(k in node for k in ("shortName", "logoUrl", "logo", "pageUrl"))
    has_player_marker = any(k in node for k in ("playerId", "shirtNumber", "positionId", "ccode"))
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
    has_team_marker = any(k in node for k in ("shortName", "logoUrl", "logo"))
    return has_player_marker and not has_team_marker


def _walk_collect_teams(node, out: dict[int, str]) -> None:
    if isinstance(node, dict):
        if _looks_like_team(node):
            try:
                out.setdefault(int(node["id"]), node["name"])
            except (TypeError, ValueError):
                pass
        for v in node.values():
            _walk_collect_teams(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_collect_teams(v, out)


def _walk_collect_players(node, out: dict[int, dict]) -> None:
    """Merge every player record we encounter, keeping the richest version."""
    if isinstance(node, dict):
        if _looks_like_player(node):
            try:
                pid = int(node["id"])
                merged = dict(out.get(pid, {}))
                for k in ("name", "playerName", "role", "ccode",
                          "cname", "country", "shirtNumber", "positionIdsDesc"):
                    if node.get(k) and not merged.get(k):
                        merged[k] = node[k]
                out[pid] = merged
            except (TypeError, ValueError):
                pass
        for v in node.values():
            _walk_collect_players(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_collect_players(v, out)


def extract_team_ids(next_data: dict) -> list[tuple[int, str]]:
    teams: dict[int, str] = {}
    _walk_collect_teams(next_data, teams)
    return sorted(teams.items(), key=lambda x: x[1].lower())


def extract_squad(next_data: dict) -> list[tuple[int, dict]]:
    players: dict[int, dict] = {}
    _walk_collect_players(next_data, players)
    return sorted(players.items(), key=lambda kv: (kv[1].get("name") or "").lower())


# ---------------------------------------------------------------------------
# Per-player page (for age + height)
# ---------------------------------------------------------------------------

def fetch_player_extras(session: requests.Session, player_id: int) -> dict:
    """Returns {country, age, height} from the player's own page."""
    nd = fetch_next_data(session, PLAYER_PAGE.format(player_id=player_id))
    if nd is None:
        return {}

    extras: dict[str, str] = {}

    def walk(n):
        if isinstance(n, dict):
            for key_list_field in ("playerInformation", "playerProps"):
                if isinstance(n.get(key_list_field), list):
                    for item in n[key_list_field]:
                        key = item.get("translationKey")
                        val = (item.get("value") or {}).get("fallback")
                        if key in ("country", "age", "height") and val and key not in extras:
                            extras[key] = val
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(nd)
    return extras


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def collect_all_players(
    session: requests.Session,
    league_slugs: Iterable[str],
) -> list[PlayerRow]:
    all_rows: list[PlayerRow] = []

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
            tnd = fetch_next_data(session, TEAM_PAGE.format(team_id=team_id))
            if tnd is None:
                logging.warning("    team %s (%d) page failed", team_name, team_id)
                continue

            squad = extract_squad(tnd)
            for pid, info in squad:
                all_rows.append(PlayerRow(
                    league=slug,
                    id=pid,
                    name=info.get("name") or info.get("playerName") or "",
                    teamId=team_id,
                    teamName=team_name,
                    role=info.get("role") or info.get("positionIdsDesc"),
                    country=info.get("cname") or info.get("ccode") or info.get("country"),
                    shirtNumber=str(info.get("shirtNumber")) if info.get("shirtNumber") is not None else None,
                ))
            logging.info("    %-30s %3d players", team_name[:30], len(squad))

    # de-dup on (league, team, player_id)
    seen: set[tuple[str, int | None, int]] = set()
    unique: list[PlayerRow] = []
    for p in all_rows:
        key = (p.league, p.teamId, p.id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def enrich_with_age_height(rows: list[PlayerRow], workers: int) -> None:
    """Mutates `rows` in place, filling country/age/height from each player page."""
    session = make_session()

    def task(row: PlayerRow) -> tuple[PlayerRow, dict]:
        return row, fetch_player_extras(session, row.id)

    total = len(rows)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(task, r) for r in rows]
        for fut in as_completed(futures):
            try:
                row, extras = fut.result()
            except Exception as e:
                logging.debug("enrich crashed: %s", e)
                done += 1
                continue
            if extras.get("country"):
                row.country = extras["country"]
            if extras.get("age"):
                row.age = extras["age"]
            if extras.get("height"):
                row.height = extras["height"]
            done += 1
            if done % 100 == 0 or done == total:
                logging.info("  enriched %d/%d", done, total)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Download every player in each league from FotMob to CSV.")
    ap.add_argument(
        "--leagues",
        nargs="+",
        choices=list(LEAGUES.keys()),
        default=list(LEAGUES.keys()),
    )
    ap.add_argument("--out", type=Path, default=Path("fotmob_league_players.csv"))
    ap.add_argument(
        "--skip-player-page",
        action="store_true",
        help="Skip per-player page fetch (drops age + height columns, much faster).",
    )
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel workers for the player-page enrichment step.")
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

    logging.info("Collecting squads from %d league(s)…", len(args.leagues))
    rows = collect_all_players(session, args.leagues)
    logging.info("Collected %d unique player records.", len(rows))

    if not rows:
        logging.error("No players found — aborting.")
        return 1

    # Checkpoint write — flushes the squad data even if enrichment crashes later
    pd.DataFrame([asdict(r) for r in rows]).to_csv(
        args.out, index=False, encoding="utf-8-sig"
    )
    logging.info("Checkpoint written → %s (squad data, no age/height yet)", args.out)

    if not args.skip_player_page:
        logging.info("Enriching with age/height from player pages (%d workers)…", args.workers)
        enrich_with_age_height(rows, args.workers)
        pd.DataFrame([asdict(r) for r in rows]).to_csv(
            args.out, index=False, encoding="utf-8-sig"
        )

    logging.info("DONE  saved_to=%s  players=%d", args.out, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
