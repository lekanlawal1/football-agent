"""Write docs/schema.md: the database description the agent's model is given, and humans can read.

Columns and types come from the live database (DESCRIBE), so the description can never drift from
what queries can actually use. The notes below add what types cannot say: meaning, units, coverage,
and the definitions the agent should apply by default.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "football.duckdb"
OUT = ROOT / "docs" / "schema.md"

TABLES = {
    "matches": "One row per match, both sources. `source` is 'statsbomb' (event data, older seasons and "
               "tournaments) or 'api-football' (2024/25 league seasons, results only). `end_minute` is the last "
               "minute played (StatsBomb only). `stage` is the round or tournament stage. "
               "`regular_season` is false for playoffs (relegation play-offs, MLS Cup playoffs) and tournaments: "
               "filter on it for league tables and 'over the season' team questions.",
    "team_match": "One row per team per match, both sources: `team`, `opponent`, `venue` ('home' or 'away'), "
                  "`goals_for`, `goals_against`, `result` ('W', 'D', 'L'). Use this for any team total or table "
                  "(goals scored, wins, points = 3 per W + 1 per D): it already counts home and away matches together. "
                  "`ht_goals_for` / `ht_goals_against` are the first-half score (StatsBomb matches only; NULL for "
                  "api-football). Second-half goals = full-time minus first-half.",
    "player_season": "One row per player, competition and season (both sources): totals and per-90 rates. "
                     "`teams` lists the club(s); a player who moved mid-season in API-Football data has one row per club. "
                     "`xg` exists for StatsBomb rows only. `position_group` is 'Goalkeeper', 'Defender', 'Midfielder' or "
                     "'Forward' (the one the player played most). Use this for season questions.",
    "player_match": "StatsBomb only: one row per player per match. `minutes` played, `started`, and match totals. "
                    "`minute_on` / `minute_off` say when the player came on and went off (`minute_off` NULL = played to the end). "
                    "`position` is StatsBomb's position in that match (e.g. 'Left Center Back', 'Right Wing'); "
                    "`position_group` is 'Goalkeeper', 'Defender' (any back, including wing backs), 'Midfielder' or 'Forward' "
                    "(wingers and strikers).",
    "events": "StatsBomb only: key on-ball events with the `minute` and `period` (1, 2; 3 and 4 are extra time) "
              "they happened in. `type` is one of: Shot, Tackle, Interception, Foul, Card, Dribble, Clearance, Block, "
              "Ball Recovery, Save, Key Pass, Own Goal. `outcome` holds e.g. 'Won' for tackles, 'Goal' for shots; "
              "`card` is 'Yellow Card', 'Second Yellow' or 'Red Card'. `x`, `y` are pitch coordinates (120 x 80, "
              "attacking towards x = 120).",
    "shots": "StatsBomb only: every shot. `statsbomb_xg` is StatsBomb's expected goals; `model_xg` is this project's "
             "own xG model (each shot scored by a model that never saw that match). `is_goal`, `body_part`, "
             "`shot_type` ('Open Play', 'Penalty', 'Free Kick', 'Corner'), `assist_type`, `distance` (yards), "
             "`angle` (radians).",
}

COVERAGE = """
## What the data covers

| Source | Competitions and seasons | Detail |
|---|---|---|
| statsbomb | Premier League, La Liga, Serie A: season '2015/2016' (all 380 matches). Ligue 1 '2015/2016' (377 of 380). Bundesliga '2015/2016' (34 matches only) and '2023/2024' (34 matches, Leverkusen's). MLS '2023' (6 matches). FIFA World Cup '2018' and '2022', UEFA Euro '2020' and '2024', Copa America '2024' | Every key event with its minute, every shot with xG, minutes played per match |
| api-football | Premier League, La Liga, Serie A, Bundesliga, Ligue 1: season '2024/2025'. MLS: season '2024' | Match results; season totals per player (tackles, goals, assists, cards, minutes...). No minute-by-minute events |

Nothing after the 2024/25 season is in the data: the free data plan stops there. Say so if asked
about later matches.

## Definitions to apply by default (say which one you used)

- **Last N minutes of a match**: second half, `period = 2 AND minute >= 90 - N` (stoppage time counts).
  Extra time (periods 3 and 4) only if the question asks for it.
- **Per 90**: total * 90 / minutes. Rank per-90 stats only among players with at least 900 minutes
  (about ten full matches) unless the question sets its own threshold, and state the threshold.
- **Tackles** means every tackle attempt (`type = 'Tackle'`), the same count as the season totals. Only
  when the question says successful or won: `outcome IN ('Won', 'Success', 'Success In Play', 'Success Out')`.
- **Goals above expected**: goals minus xG over non-penalty shots (`shot_type <> 'Penalty'`).
- **Player and team names**: match loosely and without accents, e.g.
  `strip_accents(lower(player)) LIKE '%mbappe%'`. StatsBomb uses everyday names ("Lionel Messi");
  API-Football uses short forms ("L. Messi"). Never assume one source's name format in the other.
- **Clean sheet**: the team conceded 0 (`team_match.goals_against = 0`). For a player, count it only if he
  played at least 60 minutes. For one half, use the first-half score and require him on the pitch for the
  whole half: `minute_on = 0 AND (minute_off IS NULL OR minute_off >= 45)`. Join player_match to
  team_match ON match_id AND team.
- **Positions**: "defenders" means `position_group = 'Defender'` in that match (player_match) or season
  (player_season).
- **A season named by one year** for a European league ("the 2016 Premier League season") means the
  season ending that year ('2015/2016'). Say which season you used.
- **Team totals** (goals, wins, points, goals conceded): use `team_match` and GROUP BY team. Never rank
  home_team and away_team sums separately from `matches`: that counts only half of each team's games.
- **Seasons** are text: '2015/2016', '2024/2025', '2022' (tournaments and MLS use one year).
"""


def main() -> None:
    con = duckdb.connect(str(DB), read_only=True)
    parts = ["# Database schema\n",
             "Generated by `python -m src.schema_doc` from the live database. This is exactly what the agent's "
             "model is shown.\n"]
    for t, note in TABLES.items():
        cols = con.execute(f"DESCRIBE {t}").fetchall()
        # no row counts: they change with every daily backfill, and the deploy checks this file is current
        parts.append(f"## {t}\n\n{note}\n")
        parts.append("| column | type |\n|---|---|")
        parts += [f"| {c[0]} | {c[1]} |" for c in cols]
        parts.append("")
    parts.append(COVERAGE)
    OUT.write_text("\n".join(parts))
    print(f"wrote {OUT.relative_to(ROOT)} ({len(OUT.read_text()):,} characters)")


if __name__ == "__main__":
    main()
