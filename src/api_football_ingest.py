"""Backfill API-Football match data within the free plan's 100 requests a day.

The free plan reads seasons 2022 to 2024 only (checked by src.api_football_check), so this fetches
the newest season it allows: 2024/25 for the top 5 European leagues and the 2024 MLS season.

The free plan does not allow fetching several matches per request (the ids parameter), so a match
costs one request: about 2,280 for the six seasons, roughly 25 daily runs. To be useful early:
1. Season totals first: /players returns every player's full-season statistics. The free plan
   caps it at page 3, so it is asked club by club (a squad fits in two pages of 20): about 250
   requests, three runs. Season questions work from then on.
2. Then per-match detail (events with minutes, lineups, player statistics), newest matches first
   across all leagues, so the end of the season fills in first.
- Each page and match is saved once as data/api_football/fixtures/<id>.json.gz and committed by the workflow,
  so progress survives between runs and nothing is downloaded twice.
- Each run spends at most BUDGET requests (default 90 of 100) and stops early when the API reports
  the daily limit. When everything is downloaded it spends one request (the status check) and stops.

The key comes from API_FOOTBALL_KEY and is never printed.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "api_football"
BASE = "https://v3.football.api-sports.io"
# league id: (name, season). Season is the year the season starts: 2024 = 2024/25 (MLS: 2024).
TARGETS = {39: ("Premier League", 2024), 140: ("La Liga", 2024), 135: ("Serie A", 2024),
           78: ("Bundesliga", 2024), 61: ("Ligue 1", 2024), 253: ("MLS", 2024)}
FINISHED = {"FT", "AET", "PEN"}
PAUSE = 7                  # seconds between requests: stays under the free per-minute limit


class Budget:
    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def get(self, path: str, **params) -> dict:
        if self.used >= self.limit:
            raise RuntimeError("budget")
        url = f"{BASE}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        key = os.environ["API_FOOTBALL_KEY"].strip()
        req = urllib.request.Request(url, headers={"x-apisports-key": key})
        if self.used:
            time.sleep(PAUSE)
        with urllib.request.urlopen(req, timeout=60) as r:
            self.used += 1
            body = json.load(r)
        errors = body.get("errors")
        if errors:
            # the API reports quota and plan problems in the body, with HTTP 200
            raise RuntimeError(f"API error on {path}: {errors}")
        return body


def save_gz(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(data, f, separators=(",", ":"))
    tmp.rename(path)


def load_gz(path: Path):
    with gzip.open(path, "rt") as f:
        return json.load(f)


def main(budget: int = 90) -> int:
    api = Budget(budget)
    report = []
    status = api.get("status")["response"]["requests"]
    remaining = status["limit_day"] - status["current"]
    api.limit = min(budget, remaining)
    report.append(f"Requests already used today: {status['current']} of {status['limit_day']}; "
                  f"this run may spend {api.limit} (including that check).")

    # 1. the fixture list for each league season (one request each, fetched once)
    lists = {}
    for lid, (name, season) in TARGETS.items():
        p = OUT / "lists" / f"{lid}_{season}.json.gz"
        if not p.exists():
            try:
                save_gz(p, api.get("fixtures", league=lid, season=season)["response"])
            except RuntimeError as err:
                report.append(f"Stopped while listing {name}: {err}")
                break
        lists[lid] = load_gz(p)

    # 2. transfer history first (a few requests) for players the API may have filed under a club they joined after the
    #     season (see api_football_transform.echo_suspects): one request per player, fetched once
    from src.api_football_transform import echo_suspects, player_season
    ps = player_season()
    suspects = sorted(set(echo_suspects(ps).player_id)) if not ps.empty else []
    fetched = 0
    for pid in suspects:
        p = OUT / "transfers" / f"{pid}.json.gz"
        if p.exists():
            continue
        try:
            save_gz(p, api.get("transfers", player=int(pid))["response"])
            fetched += 1
        except RuntimeError as err:
            if "budget" not in str(err):
                report.append(f"Stopped fetching transfer history: {err}")
            break
    report.append(f"Transfer history: {fetched} player(s) this run, {len(suspects)} suspected duplicate season(s) in total.")

    # 3. season totals for every player, club by club. The free plan caps /players at page 3
    #    (60 players), so a whole league cannot be paged; one club's squad fits in two pages.
    #    Team ids come from the fixture lists, so finding them costs nothing.
    teams = {}
    for lid in lists:
        for f in lists[lid]:
            for side in ("home", "away"):
                teams[(lid, f["teams"][side]["id"])] = f["teams"][side]["name"]
    pages_got, teams_done = 0, 0
    for (lid, tid), tname in sorted(teams.items()):
        season = TARGETS[lid][1]
        page = 1
        while True:
            p = OUT / "players" / f"{lid}_{season}_{tid}_{page}.json.gz"
            if p.exists():
                total_pages = load_gz(p)["paging"]["total"]
            else:
                try:
                    body = api.get("players", team=tid, season=season, page=page)
                except RuntimeError as err:
                    total_pages = None
                    if "budget" not in str(err):
                        report.append(f"Stopped at {tname}, page {page}: {err}")
                    break
                save_gz(p, {"league": lid, "team": tname, "paging": body["paging"], "response": body["response"]})
                total_pages = body["paging"]["total"]
                pages_got += 1
            if page >= min(total_pages, 3):
                teams_done += 1
                break
            page += 1
        if total_pages is None:
            break
    players_done = teams_done == len(teams)
    report.append(f"Season totals: {pages_got} page(s) this run; {teams_done} of {len(teams)} clubs complete.")

    # 4. per-match detail, newest finished matches first, one request each
    have = {int(f.name.split(".")[0]) for f in (OUT / "fixtures").glob("*.json.gz")}
    finished = [f for lid in lists for f in lists[lid] if f["fixture"]["status"]["short"] in FINISHED]
    finished.sort(key=lambda f: f["fixture"]["timestamp"], reverse=True)
    todo = [f["fixture"]["id"] for f in finished if f["fixture"]["id"] not in have]
    total = len(finished)
    got = 0
    if players_done:
        for fid in todo:
            try:
                resp = api.get("fixtures", id=fid)["response"]
            except RuntimeError as err:
                report.append(f"Stopped: {err}")
                break
            if resp:
                save_gz(OUT / "fixtures" / f"{fid}.json.gz", resp[0])
                got += 1

    done = len(have) + got
    report.append(f"Downloaded {got} matches this run. Progress: {done} of {total} finished matches "
                  f"in {len(lists)} league seasons. Requests spent this run: {api.used}.")
    if done < total:
        report.append(f"About {-(-(total - done) // 88)} more daily run(s) to finish the match detail.")
    text = "\n".join(report)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## API-Football backfill\n\n" + text.replace("\n", "\n\n") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 90))
