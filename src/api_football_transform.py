"""Turn the saved API-Football responses into tables (parquet), ready for src.build.

  api_matches         every 2024/25 league match: date, teams, score (from the fixture lists)
  api_player_season   per player per club: season totals in that league (from /players, club by club)

Only the league's own statistics are kept: asking by club returns every competition the player
played in (Champions League, cups), and those are dropped. Fields whose meaning the API does not
define clearly (passes.accuracy, rating) are left out rather than guessed at.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "api_football"
LEAGUE_NAMES = {39: "Premier League", 140: "La Liga", 135: "Serie A", 78: "Bundesliga", 61: "Ligue 1", 253: "MLS"}
FINISHED = {"FT", "AET", "PEN"}


def load_gz(p: Path):
    with gzip.open(p, "rt") as f:
        return json.load(f)


def season_label(lid: int, year: int) -> str:
    return str(year) if lid == 253 else f"{year}/{year + 1}"     # MLS runs in one calendar year


def matches() -> pd.DataFrame:
    rows = []
    for p in sorted((SRC / "lists").glob("*.json.gz")):
        lid, year = map(int, p.name.split(".")[0].split("_"))
        for f in load_gz(p):
            if f["fixture"]["status"]["short"] not in FINISHED:
                continue
            rows.append(dict(match_id=f["fixture"]["id"], competition=LEAGUE_NAMES[lid], season=season_label(lid, year),
                             date=f["fixture"]["date"][:10], home_team=f["teams"]["home"]["name"],
                             away_team=f["teams"]["away"]["name"], home_score=f["goals"]["home"],
                             away_score=f["goals"]["away"], stage=f["league"].get("round")))
    return pd.DataFrame(rows)


def n(v) -> int:
    return int(v) if v not in (None, "") else 0


def player_season() -> pd.DataFrame:
    rows = []
    for p in sorted((SRC / "players").glob("*.json.gz")):
        page = load_gz(p)
        lid = page["league"]
        for rec in page["response"]:
            pl = rec["player"]
            for st in rec["statistics"]:
                if st["league"]["id"] != lid:
                    continue                      # Champions League, cups: not this league
                g = st["games"]
                if not g.get("minutes"):
                    continue                      # in the squad, never played
                rows.append(dict(
                    player_id=pl["id"], player=pl["name"], full_name=f"{pl.get('firstname') or ''} {pl.get('lastname') or ''}".strip(),
                    country=pl.get("nationality"), age=pl.get("age"), position=g.get("position"),
                    team=st["team"]["name"], competition=LEAGUE_NAMES[lid], season=season_label(lid, st["league"]["season"]),
                    appearances=n(g.get("appearences")), starts=n(g.get("lineups")), minutes=n(g.get("minutes")),
                    goals=n(st["goals"].get("total")), assists=n(st["goals"].get("assists")),
                    penalty_goals=n(st["penalty"].get("scored")), shots=n(st["shots"].get("total")),
                    shots_on_target=n(st["shots"].get("on")), key_passes=n(st["passes"].get("key")),
                    passes=n(st["passes"].get("total")), tackles=n(st["tackles"].get("total")),
                    blocks=n(st["tackles"].get("blocks")), interceptions=n(st["tackles"].get("interceptions")),
                    duels=n(st["duels"].get("total")), duels_won=n(st["duels"].get("won")),
                    dribbles=n(st["dribbles"].get("attempts")), dribbles_completed=n(st["dribbles"].get("success")),
                    fouls=n(st["fouls"].get("committed")), fouls_won=n(st["fouls"].get("drawn")),
                    yellow_cards=n(st["cards"].get("yellow")), red_cards=n(st["cards"].get("red")) + n(st["cards"].get("yellowred")),
                    saves=n(st["goals"].get("saves")), goals_conceded=n(st["goals"].get("conceded")),
                ))
    df = pd.DataFrame(rows)
    if not df.empty:
        # the same club page can be fetched twice across runs; one row per player, club and league
        df = df.drop_duplicates(["player_id", "team", "competition", "season"])
    return df


def main() -> None:
    m, ps = matches(), player_season()
    m.to_parquet(SRC / "api_matches.parquet", index=False)
    ps.to_parquet(SRC / "api_player_season.parquet", index=False)
    print(f"api_matches: {len(m):,} rows; api_player_season: {len(ps):,} rows "
          f"from {len(list((SRC / 'players').glob('*.json.gz')))} club pages")


if __name__ == "__main__":
    main()
