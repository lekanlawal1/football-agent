"""Build the question-answering database (DuckDB) from the ingested tables, then run data tests.

Tables the agent can query (documented in docs/schema.md, which is also what the model is shown):
  matches       one row per match
  player_match  one row per player per match: minutes and totals
  events        key on-ball events with the minute they happened
  shots         every shot, with StatsBomb's xG and this project's model_xg
  player_season per player per competition season: totals and per-90 rates (a view)

A failing data test stops the build with a non-zero exit, so a bad ingest never reaches the site.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "statsbomb"
DB = ROOT / "data" / "football.duckdb"


def build(db_path: Path = DB) -> duckdb.DuckDBPyConnection:
    db_path.unlink(missing_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(f"CREATE TABLE matches AS SELECT match_id, competition, season, CAST(date AS DATE) AS date, "
                f"home_team, away_team, home_score, away_score, stage, end_minute, 'statsbomb' AS source "
                f"FROM read_parquet('{DATA / 'matches.parquet'}')")
    con.execute(f'CREATE TABLE player_match AS SELECT * EXCLUDE ("on", "off"), "on" AS minute_on, "off" AS minute_off '
                f"FROM read_parquet('{DATA / 'player_match.parquet'}')")
    con.execute(f"CREATE TABLE events AS SELECT * FROM read_parquet('{DATA / 'events.parquet'}')")
    # StatsBomb's match list and event files name a few clubs differently ("Olympique de Marseille"
    # vs "Marseille"). Use the event-file name, the one people type. A pair is only matched when it is
    # unambiguous: exactly one match-list name and one event name left over in that match.
    con.execute("""
        CREATE TEMP TABLE renames AS
        WITH t AS (SELECT match_id, list(DISTINCT team) AS teams FROM player_match GROUP BY 1),
        x AS (SELECT m.match_id, m.home_team, m.away_team,
                     list_filter(t.teams, n -> n NOT IN (m.home_team, m.away_team)) AS spare
              FROM matches m JOIN t USING (match_id))
        SELECT match_id, CASE WHEN NOT list_contains(t.teams, home_team) THEN 'home' ELSE 'away' END AS side,
               spare[1] AS event_name
        FROM x JOIN t USING (match_id)
        WHERE len(spare) = 1 AND (list_contains(t.teams, home_team) <> list_contains(t.teams, away_team))""")
    con.execute("UPDATE matches SET home_team = r.event_name FROM renames r WHERE matches.match_id = r.match_id AND r.side = 'home'")
    con.execute("UPDATE matches SET away_team = r.event_name FROM renames r WHERE matches.match_id = r.match_id AND r.side = 'away'")
    shots = DATA / "shots_xg.parquet"
    if not shots.exists():
        shots = DATA / "shots.parquet"
    con.execute(f"CREATE TABLE shots AS SELECT * FROM read_parquet('{shots}')")
    con.execute("ALTER TABLE shots ADD COLUMN IF NOT EXISTS model_xg DOUBLE")   # before src.xg has run
    con.execute("""
        CREATE VIEW player_season AS
        SELECT pm.player_id, any_value(pm.player) AS player, any_value(pm.country) AS country,
               m.competition, m.season, string_agg(DISTINCT pm.team, ', ') AS teams,
               count(*) AS appearances, sum(pm.started::INT) AS starts, sum(pm.minutes) AS minutes,
               sum(goals) AS goals, sum(penalty_goals) AS penalty_goals, sum(assists) AS assists,
               round(sum(xg), 2) AS xg, sum(shots) AS shots, sum(key_passes) AS key_passes,
               sum(passes) AS passes, sum(passes_completed) AS passes_completed,
               sum(tackles) AS tackles, sum(tackles_won) AS tackles_won, sum(interceptions) AS interceptions,
               sum(dribbles_completed) AS dribbles_completed, sum(fouls) AS fouls,
               sum(yellow_cards) AS yellow_cards, sum(red_cards) AS red_cards, sum(saves) AS saves,
               round(sum(goals) * 90.0 / nullif(sum(minutes), 0), 2) AS goals_per90,
               round(sum(xg) * 90.0 / nullif(sum(minutes), 0), 2) AS xg_per90,
               round(sum(tackles) * 90.0 / nullif(sum(minutes), 0), 2) AS tackles_per90
        FROM player_match pm JOIN matches m USING (match_id)
        GROUP BY pm.player_id, m.competition, m.season""")
    return con


# --------------------------------------------------------------------------- data tests

def run_tests(con) -> list[str]:
    """Each test returns rows that break a rule; any rows means failure."""
    tests = {
        "every match has events": "SELECT match_id FROM matches WHERE match_id NOT IN (SELECT DISTINCT match_id FROM events)",
        "team names agree across tables": """SELECT DISTINCT p.match_id, p.team FROM player_match p JOIN matches m USING (match_id)
            WHERE p.team NOT IN (m.home_team, m.away_team)""",
        "every match has 22+ players": """SELECT match_id, count(*) n FROM player_match GROUP BY 1 HAVING n < 22""",
        # goals in the data (shots that scored + own goals) add up to the official final score
        "goals add up to the final score": """
            WITH g AS (
              SELECT match_id, team, count(*) n FROM shots WHERE is_goal GROUP BY ALL
              UNION ALL
              -- an own goal is credited to the other team
              SELECT e.match_id, CASE WHEN e.team = m.home_team THEN m.away_team ELSE m.home_team END, count(*)
              FROM events e JOIN matches m USING (match_id) WHERE e.type = 'Own Goal' GROUP BY ALL)
            SELECT m.match_id, m.home_team, m.home_score, m.away_score,
                   coalesce(sum(n) FILTER (WHERE team = m.home_team), 0) AS home_found,
                   coalesce(sum(n) FILTER (WHERE team = m.away_team), 0) AS away_found
            FROM matches m LEFT JOIN g USING (match_id) GROUP BY ALL
            HAVING home_found <> m.home_score OR away_found <> m.away_score""",
        "minutes within the match": """SELECT p.match_id, p.player FROM player_match p JOIN matches m USING (match_id)
            WHERE p.minutes < 0 OR p.minutes > m.end_minute""",
        "no duplicate events": "SELECT event_id, count(*) n FROM events GROUP BY 1 HAVING n > 1",
        "no duplicate player rows": "SELECT match_id, player_id, count(*) n FROM player_match GROUP BY ALL HAVING n > 1",
        "shot xG are probabilities": """SELECT event_id FROM shots WHERE statsbomb_xg NOT BETWEEN 0 AND 1
            OR (model_xg IS NOT NULL AND model_xg NOT BETWEEN 0 AND 1)""",
        "minutes in range": "SELECT event_id FROM events WHERE minute < 0 OR minute > 130",
        "player totals match events": """
            SELECT p.match_id, p.player FROM player_match p
            LEFT JOIN (SELECT match_id, player_id, count(*) t FROM events WHERE type = 'Tackle' GROUP BY ALL) e
              USING (match_id, player_id)
            WHERE p.tackles <> coalesce(e.t, 0)""",
    }
    failures = []
    for name, sql in tests.items():
        try:
            bad = con.execute(sql).fetchall()
        except Exception as err:
            failures.append(f"{name}: query error {err}")
            continue
        status = "ok" if not bad else f"FAIL ({len(bad)} rows, e.g. {bad[:3]})"
        print(f"  {status:<8} {name}" if not bad else f"  {status} {name}")
        if bad:
            failures.append(name)
    return failures


def main() -> None:
    con = build()
    for t in ("matches", "player_match", "events", "shots"):
        print(f"{t}: {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]:,}")
    failures = run_tests(con)
    con.close()
    if failures:
        print(f"{len(failures)} data test(s) failed", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
