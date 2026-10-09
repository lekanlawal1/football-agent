/* The only SQL the Worker returns to the page: one SELECT or WITH statement, no write or system keywords. */
const DENY = /\b(drop|delete|update|insert|alter|create|replace|truncate|merge|grant|revoke|attach|detach|copy|export|import|install|load|pragma|set|reset|call|vacuum|checkpoint|begin|commit|rollback)\b/i;
// Functions that read files or URLs, or inspect the system. The agent only needs the five views;
// these would let a crafted question make the visitor's browser fetch an arbitrary address.
const DENY_FN = /\b(read_\w+|glob|pragma_\w+|duckdb_\w+|getenv|sniff_csv|parquet_\w+|iceberg_\w+|delta_scan)\s*\(/i;
export function shapeCheck(sql) {
  const bare = sql.replace(/--[^\n]*/g, " ").replace(/\/\*[\s\S]*?\*\//g, " ").trim().replace(/;\s*$/, "").trim();
  if (!bare) return "empty query";
  if (bare.includes(";")) return "more than one statement";
  if (!/^(select|with)\b/i.test(bare)) return "only SELECT queries are allowed";
  const m = bare.match(DENY);
  if (m) return `blocked keyword: ${m[0].toUpperCase()}`;
  const f = bare.match(DENY_FN);
  if (f) return `blocked function: ${f[1]}()`;
  if (/'[a-z]+:\/\//i.test(bare)) return "URLs are not allowed in queries";
  return null;
}

