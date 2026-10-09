"""Ingest StatsBomb open event data (github.com/statsbomb/open-data) into compact tables.

StatsBomb publishes every on-ball event of each match: about 3,500 events and 3 MB of JSON per
match. Keeping all of it for 1,853 matches would be 6 GB, so each match is streamed, reduced to
what questions and the xG model need, and cached as a small gzip extract. Reruns only download
matches not cached yet.

Kept per match:
  matches       one row per match (competition, season, date, teams, score, length)
  events        key on-ball events with minute and player: shots, tackles, interceptions,
                fouls, cards, dribbles, clearances, blocks, recoveries, saves, key passes
  shots         one row per shot with the features the xG model uses, plus StatsBomb's own xG
  player_match  per player per match: minutes played and totals (passes, tackles, xG, ...)

The penalty shootout (period 5) is excluded everywhere: it is not part of the match.

StatsBomb open data is free to use with attribution; see README for the licence terms.
"""

from __future__ import annotations

import gzip
import json
import math
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "statsbomb"
EXTRACT = RAW / "extracted"
OUT = ROOT / "data" / "statsbomb"
BASE = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"

# (competition_id, season_id): full league seasons first, then tournaments.
COMPETITIONS = [
    (2, 27),     # Premier League 2015/16 (complete season)
    (11, 27),    # La Liga 2015/16 (complete)
    (12, 27),    # Serie A 2015/16 (complete)
    (7, 27),     # Ligue 1 2015/16 (complete)
    (9, 27),     # Bundesliga 2015/16 (StatsBomb publishes 34 matches only)
    (9, 281),    # Bundesliga 2023/24 (Leverkusen's 34 matches)
    (44, 107),   # MLS 2023 (6 matches)
    (43, 3), (43, 106),     # World Cup 2018, 2022
    (55, 43), (55, 282),    # Euro 2020, 2024
    (223, 282),             # Copa America 2024
]

GOAL_X, GOAL_Y, POST_HALF = 120.0, 40.0, 4.0   # StatsBomb pitch is 120 x 80, attacking left to right
TACKLE_WON = {"Won", "Success", "Success In Play", "Success Out"}


def fetch_json(url: str, tries: int = 4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception:  # network blips: back off and retry
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


def name(d, key="name"):
    return d.get(key) if isinstance(d, dict) else None


# --------------------------------------------------------------------------- shot geometry

def shot_geometry(x: float, y: float) -> tuple[float, float]:
    """Distance to the goal centre (yards) and the angle the goal mouth subtends (radians)."""
    dx, dy = GOAL_X - x, GOAL_Y - y
    dist = math.hypot(dx, dy)
    a1 = math.atan2(GOAL_Y - POST_HALF - y, dx)
    a2 = math.atan2(GOAL_Y + POST_HALF - y, dx)
    return dist, abs(a2 - a1)


def in_triangle(px, py, ax, ay, bx, by, cx, cy) -> bool:
    d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
    d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
    d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def freeze_frame_features(x, y, frame) -> tuple[int | None, float | None]:
    """Defenders between the shooter and the goal mouth, and the keeper's distance from goal."""
    if not frame:
        return None, None
    blockers, gk = 0, None
    for p in frame:
        if p.get("teammate"):
            continue
        px, py = p["location"][:2]
        if name(p.get("position")) == "Goalkeeper":
            gk = math.hypot(GOAL_X - px, GOAL_Y - py)
        if in_triangle(px, py, x, y, GOAL_X, GOAL_Y - POST_HALF, GOAL_X, GOAL_Y + POST_HALF):
            blockers += 1
    return blockers, gk


# --------------------------------------------------------------------------- one match

def extract_match(match: dict) -> dict:
    mid = match["match_id"]
    events = fetch_json(f"{BASE}/events/{mid}.json")
    events = [e for e in events if e.get("period", 1) <= 4]          # drop the penalty shootout
    by_id = {e["id"]: e for e in events}
    # Everyday names: StatsBomb's player name is the full legal one ("Lionel Andres Messi Cuccittini");
    # the lineup file carries the nickname people actually use ("Lionel Messi") and nationality.
    known = {}
    for team in fetch_json(f"{BASE}/lineups/{mid}.json"):
        for p in team["lineup"]:
            known[p["player_id"]] = (p.get("player_nickname") or p["player_name"], name(p.get("country")))
    end_minute = max((e["minute"] for e in events), default=90)

    keep, shots, pm = [], [], {}

    def P(e):
        pid = name(e.get("player"), "id")
        if pid is None:
            return None
        row = pm.get(pid)
        if row is None:
            nick, country = known.get(pid, (name(e["player"]), None))
            row = pm[pid] = dict(match_id=mid, player_id=pid, player=nick, full_name=name(e["player"]),
                                 country=country, team=name(e["team"]),
                                 on=None, off=None, passes=0, passes_completed=0, key_passes=0, assists=0,
                                 shots=0, shots_on_target=0, goals=0, penalty_goals=0, xg=0.0, tackles=0,
                                 tackles_won=0, interceptions=0, fouls=0, fouls_won=0, yellow_cards=0,
                                 red_cards=0, dribbles=0, dribbles_completed=0, clearances=0, blocks=0,
                                 recoveries=0, pressures=0, saves=0)
        return row

    # who played: starting XI, substitutions, red cards
    for e in events:
        t = name(e["type"])
        if t == "Starting XI":
            for p in e["tactics"]["lineup"]:
                r = P({"player": p["player"], "team": e["team"]})
                r["on"] = 0
        elif t == "Substitution":
            off = P(e)
            off["off"] = e["minute"]
            on = P({"player": e["substitution"]["replacement"], "team": e["team"]})
            on["on"] = e["minute"]

    def add_event(e, etype, outcome=None, subtype=None, xg=None, is_goal=None, card=None):
        loc = e.get("location") or [None, None]
        keep.append(dict(match_id=mid, event_id=e["id"], period=e["period"], minute=e["minute"], second=e["second"],
                         type=etype, team=name(e["team"]), player_id=name(e.get("player"), "id"),
                         player=known.get(name(e.get("player"), "id"), (name(e.get("player")),))[0] if e.get("player") else None,
                         position=name(e.get("position")),
                         x=loc[0], y=loc[1], outcome=outcome, subtype=subtype, xg=xg, is_goal=is_goal, card=card,
                         under_pressure=bool(e.get("under_pressure")), play_pattern=name(e.get("play_pattern"))))

    for e in events:
        t = name(e["type"])
        r = P(e) if e.get("player") else None
        if t == "Pass":
            p = e["pass"]
            r["passes"] += 1
            if "outcome" not in p:
                r["passes_completed"] += 1
            if p.get("goal_assist") or p.get("shot_assist"):
                r["key_passes"] += 1
                if p.get("goal_assist"):
                    r["assists"] += 1
                add_event(e, "Key Pass", subtype="assist" if p.get("goal_assist") else "shot assist")
        elif t == "Shot":
            s = e["shot"]
            outcome = name(s.get("outcome"))
            goal = outcome == "Goal"
            r["shots"] += 1
            r["shots_on_target"] += outcome in ("Goal", "Saved", "Saved To Post")
            r["goals"] += goal
            r["penalty_goals"] += goal and name(s.get("type")) == "Penalty"
            r["xg"] += s.get("statsbomb_xg") or 0.0
            add_event(e, "Shot", outcome=outcome, subtype=name(s.get("type")), xg=s.get("statsbomb_xg"), is_goal=goal)
            x, y = e["location"][:2]
            dist, angle = shot_geometry(x, y)
            blockers, gk = freeze_frame_features(x, y, s.get("freeze_frame"))
            kp = by_id.get(s.get("key_pass_id")) if s.get("key_pass_id") else None
            kpp = kp["pass"] if kp else {}
            assist = ("none" if not kp else "through ball" if name(kpp.get("technique")) == "Through Ball"
                      else "cutback" if kpp.get("cut_back") else "cross" if kpp.get("cross")
                      else "high pass" if name(kpp.get("height")) == "High Pass" else "ground pass")
            shots.append(dict(match_id=mid, event_id=e["id"], period=e["period"], minute=e["minute"],
                              team=name(e["team"]), player_id=name(e["player"], "id"),
                              player=known.get(name(e["player"], "id"), (name(e["player"]),))[0],
                              x=x, y=y, distance=dist, angle=angle, body_part=name(s.get("body_part")),
                              shot_type=name(s.get("type")), technique=name(s.get("technique")),
                              first_time=bool(s.get("first_time")), one_on_one=bool(s.get("one_on_one")),
                              open_goal=bool(s.get("open_goal")), follows_dribble=bool(s.get("follows_dribble")),
                              under_pressure=bool(e.get("under_pressure")), play_pattern=name(e.get("play_pattern")),
                              assist_type=assist, blockers=blockers, gk_distance=gk,
                              statsbomb_xg=s.get("statsbomb_xg"), outcome=outcome, is_goal=goal))
        elif t == "Duel" and name(e["duel"].get("type")) == "Tackle":
            outcome = name(e["duel"].get("outcome"))
            r["tackles"] += 1
            r["tackles_won"] += outcome in TACKLE_WON
            add_event(e, "Tackle", outcome=outcome)
        elif t == "Interception":
            r["interceptions"] += 1
            add_event(e, "Interception", outcome=name(e["interception"].get("outcome")))
        elif t == "Foul Committed":
            r["fouls"] += 1
            card = name(e.get("foul_committed", {}).get("card"))
            add_event(e, "Foul", card=card)
        elif t == "Foul Won":
            r["fouls_won"] += 1
        elif t == "Bad Behaviour":
            card = name(e.get("bad_behaviour", {}).get("card"))
            if card:
                add_event(e, "Card", card=card)
        elif t == "Dribble":
            outcome = name(e["dribble"].get("outcome"))
            r["dribbles"] += 1
            r["dribbles_completed"] += outcome == "Complete"
            add_event(e, "Dribble", outcome=outcome)
        elif t == "Clearance":
            r["clearances"] += 1
            add_event(e, "Clearance")
        elif t == "Block":
            r["blocks"] += 1
            add_event(e, "Block")
        elif t == "Ball Recovery":
            r["recoveries"] += 1
            add_event(e, "Ball Recovery")
        elif t == "Pressure":
            r["pressures"] += 1
        elif t == "Goal Keeper" and name(e["goalkeeper"].get("type")) in ("Shot Saved", "Penalty Saved", "Shot Saved To Post"):
            r["saves"] += 1
            add_event(e, "Save", subtype=name(e["goalkeeper"].get("type")))
        elif t == "Own Goal Against":
            add_event(e, "Own Goal", is_goal=True)

    # cards: from fouls and bad behaviour; a red or second yellow ends the player's match
    for k in keep:
        if k["card"] and k["player_id"] in pm:
            r = pm[k["player_id"]]
            if k["card"] == "Yellow Card":
                r["yellow_cards"] += 1
            else:
                r["red_cards"] += 1
                r["off"] = min(r["off"] if r["off"] is not None else end_minute, k["minute"])
                if k["card"] == "Second Yellow":
                    r["yellow_cards"] += 1

    players = []
    for r in pm.values():
        if r["on"] is None:          # never on the pitch (unused sub who still appears, rare)
            continue
        off = r["off"] if r["off"] is not None else end_minute
        r["minutes"] = max(0, off - r["on"])
        r["started"] = r["on"] == 0
        r["xg"] = round(r["xg"], 4)
        players.append(r)

    m = dict(match_id=mid, competition=match["competition"]["competition_name"], season=match["season"]["season_name"],
             date=match["match_date"], kick_off=match.get("kick_off"),
             home_team=match["home_team"]["home_team_name"], away_team=match["away_team"]["away_team_name"],
             home_score=match["home_score"], away_score=match["away_score"],
             stage=name(match.get("competition_stage")), end_minute=end_minute)
    return {"match": m, "events": keep, "shots": shots, "players": players}


def cached_extract(match: dict) -> dict:
    p = EXTRACT / f"{match['match_id']}.json.gz"
    if p.exists():
        with gzip.open(p, "rt") as f:
            return json.load(f)
    data = extract_match(match)
    tmp = p.with_suffix(".tmp")
    with gzip.open(tmp, "wt") as f:
        json.dump(data, f)
    tmp.rename(p)
    return data


def main(workers: int = 12) -> None:
    EXTRACT.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    matches = []
    for cid, sid in COMPETITIONS:
        p = RAW / "matches" / f"{cid}_{sid}.json"
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(fetch_json(f"{BASE}/matches/{cid}/{sid}.json")))
        matches += json.loads(p.read_text())
    print(f"{len(matches)} matches", flush=True)

    results, failed = [], []
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(cached_extract, m): m["match_id"] for m in matches}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                results.append(fut.result())
            except Exception as err:
                failed.append((futs[fut], repr(err)))
            if i % 100 == 0:
                print(f"  {i}/{len(matches)}", flush=True)
    if failed:
        print(f"FAILED {len(failed)}: {failed[:5]}", file=sys.stderr)

    for key, fname in (("match", "matches"), ("events", "events"), ("shots", "shots"), ("players", "player_match")):
        rows = [r[key] for r in results] if key == "match" else [x for r in results for x in r[key]]
        df = pd.DataFrame(rows)
        df.to_parquet(OUT / f"{fname}.parquet", index=False)
        print(f"{fname}: {len(df):,} rows", flush=True)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
