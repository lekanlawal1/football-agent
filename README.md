# Football Stats Agent

Ask football questions in plain English and get answers computed with real SQL, with the query shown.
"Who made the most tackles from the 70th minute on in the 2015/16 Premier League?" works. So does
"Which strikers scored the most above their expected goals?" When the data cannot answer a question,
the agent says so instead of guessing.

**Status:** the data foundation and the xG model are built and tested. The agent, live data and the
web page are next (see Roadmap).

## What is in it

| Source | What | Minute by minute? | Up to date? |
|---|---|---|---|
| [StatsBomb open data](https://github.com/statsbomb/open-data) | 1,853 matches: complete 2015/16 Premier League, La Liga, Serie A and Ligue 1; World Cups 2018 and 2022; Euro 2020 and 2024; Copa America 2024; plus 34 Bundesliga and 6 MLS matches StatsBomb publishes | Yes: every tackle, shot, foul, card and more, with its minute | No, historical |
| API-Football (free plan) | Top 5 European leagues and MLS | Goals, cards and substitutions only | Being checked: the free plan limits which seasons it serves |

From StatsBomb: 676,386 key events, 46,228 shots, and minutes played for every player in every match
(52,239 player-match rows). The penalty shootout is excluded everywhere: it is not part of the match.

## Data you can trust

`python -m src.build` builds the database and runs data tests; any failure stops the build.

- **Every goal adds up.** Shots that scored plus own goals equal the official final score in all
  1,853 matches.
- The test caught a real problem: StatsBomb's match list says "Olympique de Marseille" where its
  event files say "Marseille", so a 1-1 looked like 1-0. Names are now matched across files, but only
  when the pairing is unambiguous; anything else fails the build instead of being guessed.
- Checked against the record books: Suarez 40 La Liga goals in 2015/16, Higuain 36 (the Serie A
  record), Kane 25 and Vardy 24 in the Premier League.
- Assists use StatsBomb's definition, which is slightly stricter than the official one (Suarez shows
  15 against the official 16).

## Expected goals (xG) model

The chance that a shot becomes a goal, from where and how it was taken. Rules were fixed before any
result: penalties get the observed conversion rate (74.5%) rather than a model; every shot is scored
by a model that never saw its match (5-fold cross-validation grouped by match).

| Model, 45,719 non-penalty shots | Log loss (lower is better) | Brier | ROC AUC (higher is better) |
|---|---|---|---|
| Baseline: distance and angle only | 0.278 | 0.078 | 0.746 |
| **This project: gradient boosting, all features** | **0.253** | **0.071** | **0.804** |
| StatsBomb's own professional xG, same shots | 0.249 | 0.070 | 0.812 |

It closes about 88% of the gap between the textbook baseline and StatsBomb's model, and it is well
calibrated: shots it rates at 0.39 went in 39.6% of the time. Trained on the 2015/16 leagues and
tested on international tournaments it still scores 0.787 AUC (StatsBomb: 0.798). Features: distance,
angle, defenders between the ball and the goal, the keeper's position, body part, assist type (through
ball, cutback, cross), first-time, one-on-one, under pressure. Full results: `docs/xg_results.json`.

## Run it

```bash
pip install -r requirements.txt
python -m src.statsbomb_ingest   # about 10 minutes the first time; cached after that
python -m src.xg                 # trains and validates the xG model
python -m src.build              # builds data/football.duckdb and runs the data tests
python -m pytest -q
```

## Roadmap

1. Live data: confirm what API-Football's free plan serves, then a daily update for the top 5 and MLS.
2. The agent: plain English to read-only SQL, with the guardrails from the earlier NL-to-SQL project
   (schema grounding, SELECT only, verified before it runs, row and time caps).
3. A web page that never sleeps: static page plus a small Cloudflare Worker that keeps the AI key
   private, rate-limits and caches.
4. Evaluation: 50 questions with hand-checked answers, and 30 attempts to misuse it.

## Credits

Event data: **StatsBomb**, released free for public use with attribution
([open-data](https://github.com/statsbomb/open-data), see their licence). Built by
[Lekan Lawal](https://lekanlawal1.github.io/portfolio-site/).
