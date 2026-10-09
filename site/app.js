/* Football Stats Agent page.

   1. Example questions show answers computed at build time (data/examples.json): instant, and
      written and checked by hand.
   2. Meanwhile DuckDB loads in the browser and opens the published parquet files over HTTP range
      requests, so a query downloads only the parts of the data it needs.
   3. A typed question goes to the Worker (window.AGENT_URL), which returns the model's decision.
      run_sql is checked against the real tables with EXPLAIN before it runs; if that check fails,
      the error goes back to the model once. A second failure is reported, never papered over. */

// DuckDB is imported lazily (below), so the page and the instant examples never wait on the CDN.
const DUCKDB = "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.32.0/+esm";

const $ = (id) => document.getElementById(id);
const AGENT = (window.AGENT_URL || "").replace(/\/$/, "");
const TABLES = ["matches", "player_season", "player_match", "events", "shots"];
const ROW_CAP = 200;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ------------------------------------------------------------------ rendering
function fmt(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "bigint") v = Number(v);
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("en-CA") : v.toLocaleString("en-CA", { maximumFractionDigits: 2 });
  if (v instanceof Date) return v.toISOString().slice(0, 10);
  return String(v);
}

function table(columns, rows) {
  const numeric = columns.map((_, i) => rows.length && rows.every((r) => r[i] === null || typeof r[i] === "number" || typeof r[i] === "bigint"));
  return `<div class="tablewrap"><table><thead><tr>${columns.map((c, i) => `<th class="${numeric[i] ? "n" : ""}">${esc(c.replaceAll("_", " "))}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((v, i) => `<td class="${numeric[i] ? "n" : ""}">${esc(fmt(v))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function show({ question, tag, explanation, columns, rows, sql, extra = "", meta = "" }) {
  const el = $("answer");
  el.innerHTML = `<div>${tag}</div><p class="q">${esc(question)}</p>
    ${explanation ? `<p class="expl">${esc(explanation)}</p>` : ""}
    ${extra}
    ${columns ? (rows.length ? table(columns, rows) : `<p class="expl">The query ran and found no rows.</p>`) : ""}
    ${sql ? `<details class="sql"><summary>Show the SQL that ran</summary><pre>${esc(sql)}</pre></details>` : ""}
    ${meta ? `<p class="meta">${meta}</p>` : ""}`;
  el.hidden = false;
  el.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
}
const status = (t) => { $("status").textContent = t; };

// ------------------------------------------------------------------ examples (instant)
let examples = [];
fetch("data/examples.json").then((r) => r.json()).then((list) => {
  examples = list;
  $("examples").innerHTML = list.map((x, i) => `<button type="button" data-i="${i}">${esc(x.question)}</button>`).join("");
  $("examples").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-i]");
    if (!b) return;
    const x = examples[+b.dataset.i];
    $("q").value = x.question;
    show({ question: x.question, tag: `<span class="tag inst">Instant answer</span><span class="tag ok">Checked by hand</span>`,
      explanation: x.explanation, columns: x.columns, rows: x.rows, sql: x.sql,
      meta: "Example answers are computed when the site is built, from SQL written and checked by hand. Ask your own question above to use the AI." });
  });
}).catch(() => {});

// ------------------------------------------------------------------ the in-browser database
let conn = null;
const dbReady = (async () => {
  const duckdb = await import(DUCKDB);
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  const workerUrl = URL.createObjectURL(new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" }));
  const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), new Worker(workerUrl));
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);
  conn = await db.connect();
  const base = new URL("data/", location.href).href;
  for (const t of TABLES) await conn.query(`CREATE VIEW ${t} AS SELECT * FROM read_parquet('${base}${t}.parquet')`);
  return conn;
})();
dbReady.then(() => { if (!$("status").dataset.busy) status(""); }).catch((err) => {
  status(`The in-browser database could not start (${err.message}). Example answers still work.`);
});

function rowsOf(result) {
  const cols = result.schema.fields.map((f) => f.name);
  const rows = result.toArray().map((r) => cols.map((c) => {
    const v = r[c];
    return typeof v === "bigint" ? Number(v) : v;
  }));
  return { cols, rows };
}

// ------------------------------------------------------------------ asking
async function askAgent(question, feedback) {
  const res = await fetch(`${AGENT}/ask`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, feedback }) });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

async function ask(question) {
  const go = $("go");
  go.disabled = true; $("status").dataset.busy = "1";
  const t0 = performance.now();
  try {
    if (!AGENT) {
      show({ question, tag: `<span class="tag no">Not connected yet</span>`,
        explanation: "The AI service for this page is still being deployed. Try one of the example questions, which are answered instantly." });
      return;
    }
    status("Thinking about the question...");
    let d = await askAgent(question, "");
    for (let attempt = 1; attempt <= 2; attempt++) {
      if (d.action === "clarify") {
        return show({ question, tag: `<span class="tag ask">Needs a detail</span>`, explanation: d.explanation,
          extra: `<p class="clarify">${esc(d.clarifying_question)}</p>`, meta: "Rather than guess, it asks. Add the detail to your question and ask again." });
      }
      if (d.action === "cannot_answer") {
        return show({ question, tag: `<span class="tag no">The data can't answer this</span>`, explanation: d.explanation });
      }
      if (d.action === "blocked") {
        return show({ question, tag: `<span class="tag no">Blocked</span>`, explanation: `The generated query failed a safety check (${d.detail}), so it was never run.`, sql: d.sql });
      }
      // Same shape rules as the Worker, enforced again here: the page never runs anything else.
      const { shapeCheck } = await import("./shape.js");
      const problem = shapeCheck(d.sql || "");
      if (problem) {
        return show({ question, tag: `<span class="tag no">Blocked</span>`, explanation: `The generated query failed a safety check (${problem}), so it was never run.`, sql: d.sql });
      }
      status("Loading the database in your browser...");
      const c = await dbReady;
      status("Checking the query against the real tables...");
      try {
        await c.query(`EXPLAIN ${d.sql.replace(/;\s*$/, "")}`);
      } catch (err) {
        if (attempt === 2) {
          return show({ question, tag: `<span class="tag no">Couldn't build a valid query</span>`,
            explanation: "The model's query referred to something that doesn't exist in the data, twice, so nothing was run rather than showing a guess.",
            sql: d.sql, meta: esc(String(err.message).split("\n")[0]) });
        }
        status("The first query didn't fit the data; asking the model to fix it...");
        d = await askAgent(question, String(err.message).split("\n")[0]);
        continue;
      }
      status("Running the query...");
      const result = await c.query(`SELECT * FROM (${d.sql.replace(/;\s*$/, "")}) LIMIT ${ROW_CAP + 1}`);
      const { cols, rows } = rowsOf(result);
      const capped = rows.length > ROW_CAP;
      const secs = ((performance.now() - t0) / 1000).toFixed(1);
      return show({ question, tag: `<span class="tag ok">Answered with SQL</span>${d.cached ? `<span class="tag inst">Cached</span>` : ""}${attempt === 2 ? `<span class="tag ask">Self-corrected once</span>` : ""}`,
        explanation: d.explanation, columns: cols, rows: rows.slice(0, ROW_CAP), sql: d.sql,
        meta: `${capped ? `First ${ROW_CAP} rows shown. ` : ""}Ran in your browser in ${secs}s. Check the SQL: every number comes from it.` });
    }
  } catch (err) {
    show({ question, tag: `<span class="tag no">Something went wrong</span>`, explanation: err.message });
  } finally {
    go.disabled = false; delete $("status").dataset.busy; status("");
  }
}

$("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  const ex = examples.find((x) => x.question.toLowerCase() === q.toLowerCase());
  if (ex) return document.querySelector(`#examples button[data-i="${examples.indexOf(ex)}"]`).click();
  ask(q);
});
