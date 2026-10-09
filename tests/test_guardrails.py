"""The SQL shape check exists twice: worker/src/shape.js (the live Worker and page) and its Python
mirror in src/evaluate.py (the evaluation). Both must reach the same verdict on the same cases,
and both files must name the same model."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from src.evaluate import MODEL, shape_check

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    "SELECT player FROM player_season LIMIT 5",
    "with t as (select 1) select * from t;",
    "DROP TABLE events",
    "SELECT 1; DELETE FROM events",
    "SELECT * FROM read_csv('https://evil.example/x.csv')",
    "SELECT * FROM 'https://evil.example/x.parquet'",
    "SELECT getenv('HOME')",
    "/* SELECT */ COPY events TO 'out.csv'",
    "WITH x AS (SELECT 1) SELECT * FROM x WHERE pragma_version() IS NOT NULL",
    "SELECT player FROM events WHERE type = 'Tackle' -- a comment",
    "  -- nothing \n",
    "INSTALL httpfs",
    "SELECT * FROM player_season WHERE player LIKE '%set%'",
]


def js_verdicts() -> list:
    script = ("import { shapeCheck } from './worker/src/shape.js';"
              f"console.log(JSON.stringify({json.dumps(CASES)}.map(shapeCheck)));")
    out = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_python_and_js_agree():
    try:
        js = js_verdicts()
    except FileNotFoundError:
        pytest.skip("node not installed")
    assert [shape_check(c) for c in CASES] == js


def test_plain_selects_pass_and_writes_fail():
    assert shape_check("SELECT player FROM player_season") is None
    assert shape_check("DELETE FROM events") is not None
    assert shape_check("SELECT * FROM read_parquet('x')") is not None


def test_word_inside_a_string_is_fine():
    # 'set' inside a LIKE pattern is not the SET statement... but the denylist is deliberately blunt.
    assert shape_check("SELECT * FROM player_season WHERE player LIKE '%set%'") is not None


def test_same_model_in_worker_and_evaluation():
    worker = (ROOT / "worker" / "src" / "index.js").read_text()
    assert re.search(r'const MODEL = "([^"]+)"', worker).group(1) == MODEL
