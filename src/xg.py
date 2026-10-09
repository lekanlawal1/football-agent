"""Expected goals (xG): the chance that a shot becomes a goal, from where and how it was taken.

Rules fixed before looking at any result:
- Penalties are not modelled: every model, StatsBomb's included, gives them one fixed value, the
  observed conversion rate. Own goals are not shots.
- Out-of-fold only: 5-fold cross-validation grouped by match, so a shot is always scored by a
  model that never saw that match (shots in one match are not independent).
- Two models: a logistic regression on distance and angle (the textbook baseline), and gradient
  boosting on every feature. Both are compared with StatsBomb's own xG on the same shots.
- Metrics: log loss and Brier score (lower is better: they reward honest probabilities, not just
  ranking), ROC AUC (ranking), and calibration (does 0.2 xG score about 20% of the time?).
- Generalisation check: train on the 2015/16 league seasons, test on international tournaments.

Writes data/statsbomb/shots_xg.parquet (every shot with model_xg) and docs/xg_results.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "statsbomb"
DOCS = ROOT / "docs"
SEED = 7

NUMERIC = ["distance", "angle", "blockers", "gk_distance", "gk_missing"]
FLAGS = ["first_time", "one_on_one", "open_goal", "follows_dribble", "under_pressure"]
CATEGORICAL = ["body_part", "shot_type", "play_pattern", "assist_type"]
LEAGUES_2015 = {"Premier League", "La Liga", "Serie A", "Ligue 1", "1. Bundesliga"}


def load() -> pd.DataFrame:
    shots = pd.read_parquet(DATA / "shots.parquet")
    matches = pd.read_parquet(DATA / "matches.parquet")[["match_id", "competition", "season"]]
    df = shots.merge(matches, on="match_id", how="left")
    df["gk_missing"] = df["gk_distance"].isna().astype(int)
    df["gk_distance"] = df["gk_distance"].fillna(df["gk_distance"].median())
    df["blockers"] = df["blockers"].fillna(df["blockers"].median())
    for c in FLAGS:
        df[c] = df[c].astype(int)
    body = df["body_part"].fillna("Other")
    df["body_part"] = np.where(body.str.contains("Foot"), "Foot", np.where(body == "Head", "Head", "Other"))
    df["is_goal"] = df["is_goal"].astype(int)
    return df


def baseline():
    return make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))


def boosted():
    pre = ColumnTransformer([("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=20), CATEGORICAL)],
                            remainder="passthrough", sparse_threshold=0)
    gb = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=24,
                                        min_samples_leaf=60, l2_regularization=1.0, random_state=SEED)
    return make_pipeline(pre, gb)


def scores(y, p) -> dict:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return {"log_loss": round(log_loss(y, p), 4), "brier": round(brier_score_loss(y, p), 4),
            "roc_auc": round(roc_auc_score(y, p), 4), "mean_predicted": round(float(p.mean()), 4),
            "goal_rate": round(float(np.mean(y)), 4)}


def calibration(y, p, bins=10) -> list[dict]:
    d = pd.DataFrame({"y": y, "p": p})
    d["bin"] = pd.qcut(d["p"], bins, labels=False, duplicates="drop")
    g = d.groupby("bin").agg(shots=("y", "size"), predicted=("p", "mean"), actual=("y", "mean"))
    return [{k: round(float(v), 4) if k != "shots" else int(v) for k, v in r.items()} for r in g.reset_index(drop=True).to_dict("records")]


def out_of_fold(model_fn, X, y, groups) -> np.ndarray:
    pred = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups):
        m = model_fn().fit(X.iloc[tr], y.iloc[tr])
        pred[te] = m.predict_proba(X.iloc[te])[:, 1]
    return pred


def main() -> dict:
    df = load()
    pens = df["shot_type"] == "Penalty"
    pen_rate = float(df.loc[pens, "is_goal"].mean())
    np_df = df.loc[~pens].reset_index(drop=True)
    y, groups = np_df["is_goal"], np_df["match_id"]

    X_base = np_df[["distance", "angle"]]
    X_full = np_df[NUMERIC + FLAGS + CATEGORICAL]
    p_base = out_of_fold(baseline, X_base, y, groups)
    p_full = out_of_fold(boosted, X_full, y, groups)
    has_sb = np_df["statsbomb_xg"].notna()
    p_sb = np_df["statsbomb_xg"].fillna(np_df["statsbomb_xg"].mean()).to_numpy()

    results = {
        "shots": int(len(df)), "non_penalty_shots": int(len(np_df)), "penalties": int(pens.sum()),
        "penalty_conversion": round(pen_rate, 4), "matches": int(df["match_id"].nunique()),
        "cross_validated": {
            "baseline_distance_angle": scores(y, p_base),
            "gradient_boosting_all_features": scores(y, p_full),
            "statsbomb_xg": scores(y[has_sb], p_sb[has_sb]),
        },
        "calibration_gradient_boosting": calibration(y, p_full),
    }

    # generalisation: leagues 2015/16 -> international tournaments
    train = np_df["competition"].isin(LEAGUES_2015) & (np_df["season"] == "2015/2016")
    test = ~np_df["competition"].isin(LEAGUES_2015 | {"Major League Soccer"})
    m = boosted().fit(X_full[train], y[train])
    p_t = m.predict_proba(X_full[test])[:, 1]
    results["leagues_to_tournaments"] = {
        "train_shots": int(train.sum()), "test_shots": int(test.sum()),
        "gradient_boosting": scores(y[test], p_t), "statsbomb_xg": scores(y[test], p_sb[test.to_numpy()]),
    }

    # every shot gets an out-of-fold model xG; penalties get the observed conversion rate
    out = df.copy()
    out["model_xg"] = pen_rate
    out.loc[~pens, "model_xg"] = p_full
    out.drop(columns=["gk_missing"]).to_parquet(DATA / "shots_xg.parquet", index=False)

    DOCS.mkdir(exist_ok=True)
    (DOCS / "xg_results.json").write_text(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
