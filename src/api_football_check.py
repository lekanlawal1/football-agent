"""What does the API-Football free plan actually give us? Run once in GitHub Actions.

Spends about 10 of the 100 free daily requests. Reports, for the top 5 European leagues and MLS:
the seasons the plan can read, each season's coverage flags (player statistics per match,
events with minutes, lineups), and whether a recent finished match really returns per-player
tackles. The key comes from the API_FOOTBALL_KEY environment variable and is never printed.

Writes the report to stdout and, in Actions, to the job summary.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

BASE = "https://v3.football.api-sports.io"
LEAGUES = {39: "Premier League", 140: "La Liga", 135: "Serie A", 78: "Bundesliga", 61: "Ligue 1", 253: "MLS"}


def get(path: str, **params) -> dict:
    url = f"{BASE}/{path}" + (("?" + urllib.parse.urlencode(params)) if params else "")
    # strip(): a key pasted into GitHub's secret box can carry a stray newline
    req = urllib.request.Request(url, headers={"x-apisports-key": os.environ["API_FOOTBALL_KEY"].strip()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    if not os.environ.get("API_FOOTBALL_KEY", "").strip():
        print("API_FOOTBALL_KEY is not set", file=sys.stderr)
        return 1
    out = []
    status = get("status")
    acct = status.get("response") or {}
    sub, req = acct.get("subscription", {}), acct.get("requests", {})
    out.append(f"## API-Football free plan check\n\nPlan: **{sub.get('plan')}**, active: {sub.get('active')}. "
               f"Requests today: {req.get('current')} of {req.get('limit_day')}.\n")
    if status.get("errors"):
        out.append(f"Errors: `{status['errors']}`\n")

    out.append("| League | Current season | Seasons listed | Player stats per match | Events | Lineups |\n|---|---|---|---|---|---|")
    current = {}
    for lid, label in LEAGUES.items():
        r = get("leagues", id=lid)
        if r.get("errors"):
            out.append(f"| {label} | error: {r['errors']} | | | | |")
            continue
        seasons = r["response"][0]["seasons"] if r.get("response") else []
        cur = next((s for s in seasons if s.get("current")), None)
        years = [s["year"] for s in seasons]
        if cur:
            current[lid] = cur["year"]
            cov = cur.get("coverage", {})
            fx = cov.get("fixtures", {})
            out.append(f"| {label} | {cur['year']} | {min(years)} to {max(years)} | "
                       f"{fx.get('statistics_players')} | {fx.get('events')} | {fx.get('lineups')} |")
        else:
            out.append(f"| {label} | none flagged current | {years[:3]}... | | | |")

    # Can the free plan actually read the current season? Try the Premier League's fixtures.
    season = current.get(39)
    out.append(f"\n### Can the plan read the current season ({season})?\n")
    fx = get("fixtures", league=39, season=season) if season else {"errors": "no current season"}
    if fx.get("errors"):
        out.append(f"**No.** The API answered: `{fx['errors']}`")
        # try the most recent season the free plan allows, so we know what history is reachable
        for yr in (season - 1 if season else 2025, 2024, 2023):
            t = get("fixtures", league=39, season=yr)
            ok = not t.get("errors") and t.get("results", 0) > 0
            out.append(f"\n- Season {yr}: {'readable, ' + str(t.get('results')) + ' fixtures' if ok else 'not readable: ' + str(t.get('errors'))}")
            if ok:
                break
    else:
        done = [f for f in fx.get("response", []) if f["fixture"]["status"]["short"] in ("FT", "AET", "PEN")]
        out.append(f"**Yes.** {fx.get('results')} fixtures listed, {len(done)} finished.")
        if done:
            last = max(done, key=lambda f: f["fixture"]["timestamp"])
            fid = last["fixture"]["id"]
            ps = get("fixtures/players", fixture=fid)
            teams = ps.get("response", [])
            players = [p for t in teams for p in t.get("players", [])]
            with_tackles = [p for p in players if (p["statistics"][0].get("tackles") or {}).get("total") is not None]
            out.append(f"\nLatest finished match: {last['teams']['home']['name']} {last['goals']['home']}-"
                       f"{last['goals']['away']} {last['teams']['away']['name']} ({last['fixture']['date'][:10]}). "
                       f"Players returned: {len(players)}, with a tackles figure: {len(with_tackles)}.")
            if players:
                keys = sorted(players[0]["statistics"][0].keys())
                out.append(f"\nPer-player stat groups available: {', '.join(keys)}.")

    after = get("status").get("response", {}).get("requests", {})
    out.append(f"\nRequests used today after this check: {after.get('current')} of {after.get('limit_day')}.")
    report = "\n".join(out)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
