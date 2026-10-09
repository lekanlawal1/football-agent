/* Football Stats Agent: the only server-side piece.

   The page sends a question; this Worker asks Gemini for a decision (run_sql, clarify or
   cannot_answer) and returns it. The SQL itself runs in the visitor's browser (DuckDB-WASM on the
   published data files), so there is no database here to protect or to keep awake.

   What this Worker is for:
   - the Gemini key stays secret (an encrypted Worker secret, never sent to the browser)
   - rate limiting per visitor, so nobody can burn through the free model quota
   - caching: the same question gets the same answer without another model call
   - a shape check: only a single SELECT / WITH statement is ever returned to the page */

import PROMPT from "../../agent/system_prompt.md";
import SCHEMA from "../../docs/schema.md";
import { shapeCheck } from "./shape.js";

const MODEL = "gemini-3-flash-preview";
const MAX_QUESTION = 300;
const CACHE_DAYS = 7;
const ALLOWED_ORIGINS = ["https://lekanlawal1.github.io", "http://localhost:8770"];

const SYSTEM = PROMPT.replace("{schema}", SCHEMA);
// Fingerprint of the model, prompt and schema (FNV-1a), part of the cache key: a deploy that changes
// any of them never serves answers made under the old version.
const VERSION = (() => {
  let h = 0x811c9dc5;
  for (const ch of MODEL + SYSTEM) h = Math.imul(h ^ ch.charCodeAt(0), 0x01000193) >>> 0;
  return h.toString(36);
})();
const RESPONSE_SCHEMA = {
  type: "OBJECT",
  properties: {
    action: { type: "STRING", enum: ["run_sql", "clarify", "cannot_answer"] },
    sql: { type: "STRING", description: "One DuckDB SELECT statement. Empty unless action is run_sql." },
    explanation: { type: "STRING", description: "One or two plain sentences: what was computed, the season, defaults used; or why it cannot be answered." },
    clarifying_question: { type: "STRING", description: "Empty unless action is clarify." },
  },
  required: ["action", "explanation"],
};

function cors(origin) {
  const ok = ALLOWED_ORIGINS.includes(origin);
  return {
    "Access-Control-Allow-Origin": ok ? origin : ALLOWED_ORIGINS[0],
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Vary": "Origin",
  };
}

function json(body, status, origin, extra = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...cors(origin), ...extra } });
}

async function askGemini(env, question, feedback) {
  let text = `Question: ${question}`;
  if (feedback) text += `\n\nYour previous query failed when it was checked against the database:\n${feedback}\nFix it using only schema columns, or choose cannot_answer.`;
  const res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-goog-api-key": env.GEMINI_API_KEY },
    body: JSON.stringify({
      systemInstruction: { parts: [{ text: SYSTEM }] },
      contents: [{ role: "user", parts: [{ text }] }],
      generationConfig: { responseMimeType: "application/json", responseSchema: RESPONSE_SCHEMA, temperature: 0,
        thinkingConfig: { thinkingLevel: "low" } },
    }),
  });
  if (!res.ok) throw new Error(`model HTTP ${res.status}`);
  const data = await res.json();
  const part = data.candidates?.[0]?.content?.parts?.find((p) => p.text);
  if (!part) throw new Error("model returned no answer");
  return JSON.parse(part.text);
}

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    if (request.method === "OPTIONS") return new Response(null, { headers: cors(origin) });
    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true, model: MODEL }, 200, origin);
    if (request.method !== "POST" || url.pathname !== "/ask") return json({ error: "not found" }, 404, origin);

    let body;
    try { body = await request.json(); } catch { return json({ error: "send JSON: {question}" }, 400, origin); }
    const question = String(body.question || "").trim().slice(0, MAX_QUESTION);
    const feedback = String(body.feedback || "").slice(0, 500);
    if (!question) return json({ error: "empty question" }, 400, origin);

    // Cache first: a repeated question costs nothing and does not count against the visitor.
    const key = new Request(`https://cache.football-agent/${VERSION}/${encodeURIComponent(question.toLowerCase().replace(/\s+/g, " "))}`);
    const cache = caches.default;
    if (!feedback) {
      const hit = await cache.match(key);
      if (hit) return json({ ...(await hit.json()), cached: true }, 200, origin);
    }

    if (env.RATE_LIMITER) {
      const ip = request.headers.get("CF-Connecting-IP") || "unknown";
      const { success } = await env.RATE_LIMITER.limit({ key: ip });
      if (!success) return json({ error: "Too many questions in a minute. Try again shortly." }, 429, origin);
    }

    let d;
    try { d = await askGemini(env, question, feedback); }
    catch (err) { return json({ error: `The model is unavailable right now (${err.message}). Try an example question.` }, 502, origin); }

    const out = { action: d.action, sql: d.sql || "", explanation: d.explanation || "", clarifying_question: d.clarifying_question || "" };
    if (out.action === "run_sql") {
      const problem = shapeCheck(out.sql);
      if (problem) Object.assign(out, { action: "blocked", detail: problem });
    }
    if (!feedback && out.action !== "blocked") {
      ctx.waitUntil(cache.put(key, new Response(JSON.stringify(out), { headers: { "Cache-Control": `max-age=${CACHE_DAYS * 86400}` } })));
    }
    return json(out, 200, origin);
  },
};
