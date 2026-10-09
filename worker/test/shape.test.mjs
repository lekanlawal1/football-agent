// The Worker only ever returns a single SELECT / WITH statement to the page.
import { test } from "node:test";
import assert from "node:assert/strict";
import { shapeCheck } from "../src/shape.js";

test("allows a plain SELECT and a WITH query", () => {
  assert.equal(shapeCheck("SELECT player FROM player_season LIMIT 5"), null);
  assert.equal(shapeCheck("with t as (select 1) select * from t;"), null);
});
test("rejects writes, second statements and settings", () => {
  assert.match(shapeCheck("DROP TABLE events"), /only SELECT/);
  assert.match(shapeCheck("SELECT 1; DELETE FROM events"), /more than one statement/);
  assert.match(shapeCheck("SELECT * FROM read_parquet('x') ; ATTACH 'y'"), /more than one/);
  assert.match(shapeCheck("WITH x AS (SELECT 1) SELECT * FROM x WHERE pragma_version() IS NOT NULL"), /blocked function/);
  assert.match(shapeCheck("select * from events where type = 'x' -- \n; install httpfs"), /more than one|INSTALL/);
});
test("comments cannot hide a statement", () => {
  assert.match(shapeCheck("/* SELECT */ COPY events TO 'out.csv'"), /only SELECT/);
});
test("empty input", () => {
  assert.equal(shapeCheck("  -- nothing \n"), "empty query");
});
test("no file or URL reading functions", () => {
  assert.match(shapeCheck("SELECT * FROM read_csv('https://evil.example/x.csv')"), /blocked function: read_csv/);
  assert.match(shapeCheck("SELECT * FROM 'https://evil.example/x.parquet'"), /URLs are not allowed/);
  assert.match(shapeCheck("SELECT getenv('HOME')"), /blocked function: getenv/);
  assert.equal(shapeCheck("SELECT player, sum(goals) AS goals FROM player_season GROUP BY player"), null);
});
