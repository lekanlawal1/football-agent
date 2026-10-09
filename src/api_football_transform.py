"""Turn the saved API-Football responses into tables (parquet), ready for src.build.

  api_matches         every 2024/25 league match: date, teams, score (from the fixture lists)
  api_player_season   per player per club: season totals in that league (from /players, club by club)

Only the league's own statistics are kept: asking by club returns every competition the player
played in (Champions League, cups), and those are dropped.

Summer transfers: after a player moves, the API also files his finished season under his new club
(Matheus Cunha's 2024/25 Wolves season appears again under Manchester United, 15 goals both times).
`echo_suspects` finds such pairs (same league season, same goals, near-identical minutes); the ingest
fetches those players' transfer history, and a row is dropped when the player joined that club after
the season's last match. A genuine mid-season move (Rashford: different numbers at each club) is kept. Fields whose meaning the API does not
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
                    team=st["team"]["name"], team_id=st["team"]["id"], competition=LEAGUE_NAMES[lid], season=season_label(lid, st["league"]["season"]),
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
        df = drop_echoes(df)
    return df


def echo_suspects(df: pd.DataFrame) -> pd.DataFrame:
    """Pairs of rows for one player and league season at two clubs with the same goals and minutes
    within 4% (or 15 minutes): one is probably the API re-filing the season under a later club."""
    pairs = df.merge(df, on=["player_id", "competition", "season"], suffixes=("_a", "_b"))
    pairs = pairs[pairs.team_id_a < pairs.team_id_b]
    close = (pairs.minutes_a - pairs.minutes_b).abs() <= (pairs[["minutes_a", "minutes_b"]].max(axis=1) * 0.04).clip(lower=15)
    return pairs[close & (pairs.goals_a == pairs.goals_b)]


def season_ends() -> dict[str, str]:
    """Date of each league season's last match, from the fixture lists."""
    m = matches()
    return m.groupby(["competition", "season"]).date.max().to_dict() if not m.empty else {}


def joined_after(player_id: int, team_id: int, after: str) -> bool | None:
    """True if the player's transfer history shows him joining team_id after the given date; None if
    the history has not been downloaded yet."""
    p = SRC / "transfers" / f"{player_id}.json.gz"
    if not p.exists():
        return None
    for rec in load_gz(p):
        for t in rec.get("transfers", []):
            if (t.get("teams", {}).get("in") or {}).get("id") == team_id and (t.get("date") or "") > after:
                return True
    return False


def drop_echoes(df: pd.DataFrame) -> pd.DataFrame:
    ends = season_ends()
    drop, unresolved = set(), []
    for _, r in echo_suspects(df).iterrows():
        end = ends.get((r.competition, r.season), "9999")
        found = False
        for side in ("a", "b"):
            if joined_after(r.player_id, r[f"team_id_{side}"], end):
                drop.add((r.player_id, r[f"team_id_{side}"], r.competition, r.season))
                found = True
        if not found:
            unresolved.append(f"{r.player_a} ({r.team_a} / {r.team_b})")
    if drop:
        key = list(zip(df.player_id, df.team_id, df.competition, df.season))
        df = df[[k not in drop for k in key]]
    print(f"summer-transfer echoes: {len(drop)} dropped, {len(unresolved)} awaiting transfer history"
          + (f": {', '.join(unresolved[:8])}" if unresolved else ""))
    return df


def main() -> None:
    m, ps = matches(), player_season()
    m.to_parquet(SRC / "api_matches.parquet", index=False)
    ps.to_parquet(SRC / "api_player_season.parquet", index=False)
    print(f"api_matches: {len(m):,} rows; api_player_season: {len(ps):,} rows "
          f"from {len(list((SRC / 'players').glob('*.json.gz')))} club pages")


if __name__ == "__main__":
    main()
