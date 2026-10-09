"""Export the database to the web page: parquet tables the browser queries, and example answers.

- Tables go to site/data/*.parquet. The browser's DuckDB reads them with HTTP range requests, so a
  query downloads only the columns and row groups it needs, not the whole 30 MB of events. Rows are
  sorted by the columns questions filter on most (event type, competition, season), so the row-group
  statistics let DuckDB skip most of the file.
- site/data/examples.json holds the example questions with answers computed here, from SQL written
  and checked by hand. They appear instantly, before the in-browser database has even loaded.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "football.duckdb"
OUT = ROOT / "site" / "data"

EXPORTS = {
    "matches": "SELECT * FROM matches ORDER BY source, competition, season, date",
    "team_match": "SELECT * FROM team_match ORDER BY source, competition, season, team, date",
    "player_season": "SELECT * FROM player_season ORDER BY source, competition, season, player",
    "player_match": "SELECT * FROM player_match ORDER BY match_id, team",
    "events": "SELECT e.* FROM events e JOIN matches m USING (match_id) ORDER BY e.type, m.competition, m.season, e.match_id, e.minute",
    "shots": "SELECT s.* FROM shots s JOIN matches m USING (match_id) ORDER BY m.competition, m.season, s.match_id",
}

EXAMPLES = [
    ("Who made the most tackles in the last 20 minutes of Premier League matches in 2015/16?",
     "Tackles in the second half from the 70th minute on (stoppage time included), Premier League 2015/16.",
     """SELECT e.player, e.team, count(*) AS tackles_last_20
        FROM events e JOIN matches m USING (match_id)
        WHERE m.competition = 'Premier League' AND m.season = '2015/2016'
          AND e.type = 'Tackle' AND e.period = 2 AND e.minute >= 70
        GROUP BY e.player, e.team ORDER BY tackles_last_20 DESC LIMIT 10"""),
    ("Who had the most tackles in the Premier League in 2024/25?",
     "Season totals for the 2024/25 Premier League, with minutes played and tackles per 90.",
     """SELECT player, teams AS team, minutes, tackles, tackles_per90
        FROM player_season WHERE competition = 'Premier League' AND season = '2024/2025'
        ORDER BY tackles DESC LIMIT 10"""),
    ("Which strikers scored the most goals above their expected goals in 2015/16?",
     "Non-penalty goals minus this project's xG, all four complete 2015/16 leagues, players with at least 40 shots.",
     """SELECT s.player, any_value(s.team) AS team, m.competition, count(*) AS shots,
               sum(s.is_goal::INT) AS goals, round(sum(s.model_xg), 1) AS xg,
               round(sum(s.is_goal::INT) - sum(s.model_xg), 1) AS goals_above_xg
        FROM shots s JOIN matches m USING (match_id)
        WHERE m.season = '2015/2016' AND s.shot_type <> 'Penalty'
        GROUP BY s.player, m.competition HAVING count(*) >= 40
        ORDER BY goals_above_xg DESC LIMIT 10"""),
    ("Who scored the most goals at the 2022 World Cup?",
     "Goals at the 2022 FIFA World Cup, penalties included, shootouts excluded.",
     """SELECT p.player, p.country, sum(p.goals) AS goals, sum(p.penalty_goals) AS penalties,
               sum(p.assists) AS assists, sum(p.minutes) AS minutes
        FROM player_match p JOIN matches m USING (match_id)
        WHERE m.competition = 'FIFA World Cup' AND m.season = '2022'
        GROUP BY p.player, p.country ORDER BY goals DESC, assists DESC LIMIT 10"""),
    ("Which MLS players created the most chances in 2024?",
     "Key passes (passes leading to a shot) in the 2024 MLS season, with assists and minutes.",
     """SELECT player, teams AS team, minutes, key_passes, assists,
               round(key_passes * 90.0 / nullif(minutes, 0), 2) AS key_passes_per90
        FROM player_season WHERE competition = 'MLS' AND season = '2024'
        ORDER BY key_passes DESC LIMIT 10"""),
]


def clean(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB), read_only=True)
    for name, sql in EXPORTS.items():
        con.execute(f"COPY ({sql}) TO '{OUT / name}.parquet' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 50000)")
        print(f"{name}.parquet  {(OUT / f'{name}.parquet').stat().st_size / 1e6:.1f} MB")
    examples = []
    for q, expl, sql in EXAMPLES:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = [[clean(v) for v in r] for r in cur.fetchall()]
        if not rows:
            continue   # a source not loaded yet (e.g. season totals still backfilling): leave it out
        examples.append({"question": q, "explanation": expl, "sql": " ".join(sql.split()), "columns": cols, "rows": rows})
    (OUT / "examples.json").write_text(json.dumps(examples))
    built = con.execute("SELECT max(date) FILTER (WHERE source = 'api-football'), count(*) FROM matches").fetchone()
    # Coverage for the page's "what you can ask" guide, so it always matches the published data
    coverage = [dict(zip(("competition", "season", "source", "matches", "player_rows"), r)) for r in con.execute("""
        SELECT m.competition, m.season, m.source, count(DISTINCT m.match_id),
               (SELECT count(*) FROM player_season p WHERE p.competition = m.competition AND p.season = m.season)
        FROM matches m GROUP BY ALL ORDER BY m.source DESC, m.competition, m.season""").fetchall()]
    (OUT / "meta.json").write_text(json.dumps({"latest_match": clean(built[0]), "matches": built[1],
                                               "coverage": coverage}))
    print(f"examples.json  {len(examples)} examples")


if __name__ == "__main__":
    main()
