// Generate the schema pack from packages/core/src/schema.ts (the single source) and the MCP tool definitions.
//   node tools/gen-schemas.mjs        writes schema/orp-0.1/*.schema.json, schema/openapi.json, schema/mcp-tools.json
//   node tools/gen-schemas.mjs --check   fails if the committed files differ (CI)
import { mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { schema } from "../packages/core/dist/index.js";
import { TOOLS } from "../packages/server/dist/mcp.js";

const BASE = "https://orp.dev/schema/0.1/"; // placeholder host, renamed with the project
const out = {};
for (const [name, s] of Object.entries(schema.SCHEMAS)) {
  out[`schema/orp-0.1/${name}.schema.json`] = { $schema: "https://json-schema.org/draft/2020-12/schema", $id: BASE + name, title: name, ...s };
}
const ref = (n) => ({ $ref: `#/components/schemas/${n}` });
const json = (n) => ({ content: { "application/json": { schema: ref(n) } } });
const ok = (n, d = "OK") => ({ "200": { description: d, ...json(n) }, default: { description: "error", ...json("Error") } });
const p = (summary, verb, body, resp, extra = {}) => ({ [verb]: { summary, security: [{ bearer: [] }], ...(body ? { requestBody: { required: true, ...json(body) } } : {}), responses: ok(resp), ...extra } });
const tenant = { name: "tenant", in: "path", required: true, schema: { type: "string" }, description: "must equal the token's tenant" };
const id = { name: "id", in: "path", required: true, schema: { type: "string" } };
const q = (name, type = "string") => ({ name, in: "query", schema: { type } });
const generic = { type: "object" };
const openapi = {
  openapi: "3.1.0",
  info: { title: "ORP HTTP binding", version: "0.1", description: "Open Recall Protocol (working name) HTTP binding, spec §8.2. The principal always comes from the bearer token." },
  servers: [{ url: "http://127.0.0.1:8787" }],
  components: { securitySchemes: { bearer: { type: "http", scheme: "bearer", description: "API key (orp_...) or an OAuth 2.1 / OIDC access token" } },
    schemas: { ...schema.SCHEMAS, Generic: generic } },
  paths: {
    "/v1/{tenant}/capabilities": { parameters: [tenant], ...p("Engine capabilities", "get", null, "Generic") },
    "/v1/{tenant}/whoami": { parameters: [tenant], ...p("The principal this token resolves to (extension x-whoami)", "get", null, "Generic") },
    "/v1/{tenant}/remember": { parameters: [tenant], ...p("Write memories", "post", "RememberRequest", "RememberResponse", { parameters: [{ name: "If-Match", in: "header", schema: { type: "string" } }] }) },
    "/v1/{tenant}/recall": { parameters: [tenant], ...p("Recall a context pack", "post", "RecallRequest", "ContextPack") },
    "/v1/{tenant}/profile": { parameters: [tenant], ...p("Always-loaded context within a budget", "post", "ProfileRequest", "Generic") },
    "/v1/{tenant}/forget": { parameters: [tenant], ...p("Retract, redact or purge (cascades)", "post", "ForgetRequest", "ForgetResponse") },
    "/v1/{tenant}/feedback": { parameters: [tenant], ...p("Usage and draft-to-final feedback", "post", "FeedbackRequest", "Generic") },
    "/v1/{tenant}/reflect": { parameters: [tenant], ...p("Consolidate threads into summaries", "post", "ReflectRequest", "Generic") },
    "/v1/{tenant}/link": { parameters: [tenant], ...p("Create a typed edge", "post", "LinkRequest", "Generic") },
    "/v1/{tenant}/import": { parameters: [tenant], ...p("Import a bundle (tenant admins)", "post", "Bundle", "Generic") },
    "/v1/{tenant}/export": { parameters: [tenant], ...p("Export a lossless bundle", "get", null, "Bundle") },
    "/v1/{tenant}/memories": { parameters: [tenant, q("kind"), q("thread"), q("scope"), q("limit", "integer"), q("cursor", "integer")], ...p("List admissible records (extension x-list)", "get", null, "Generic") },
    "/v1/{tenant}/memories/{id}": { parameters: [tenant, id], ...p("Get a record (ETag carries the version)", "get", null, "Memory"),
      ...p("Retract a record (If-Match guards the version)", "delete", null, "ForgetResponse") },
    "/v1/{tenant}/memories/{id}/neighbours": { parameters: [tenant, id, q("before", "integer"), q("after", "integer")], ...p("Units before and after in the thread", "get", null, "Generic") },
    "/v1/{tenant}/memories/{id}/history": { parameters: [tenant, id], ...p("Version chain of this record's claim", "get", null, "Generic") },
    "/v1/{tenant}/memories/{id}/edges": { parameters: [tenant, id], ...p("Edges touching this record", "get", null, "Generic") },
    "/v1/{tenant}/memories/{id}/approve": { parameters: [tenant, id], ...p("Approve a draft", "post", null, "Generic") },
    "/v1/{tenant}/history": { parameters: [tenant, q("subject"), q("predicate")], ...p("Version chain of a claim key", "get", null, "Generic") },
    "/v1/{tenant}/threads/{thread}/neighbours": { parameters: [tenant, { name: "thread", in: "path", required: true, schema: { type: "string" } }], ...p("A whole thread, admissible units only", "get", null, "Generic") },
    "/v1/{tenant}/changes": { parameters: [tenant, q("cursor", "integer"), q("limit", "integer")], ...p("Change feed after a cursor", "get", null, "ChangesResponse") },
    "/v1/{tenant}/audit": { parameters: [tenant], ...p("Audit log (auditor role)", "get", null, "Generic") },
    "/v1/{tenant}/jobs/{id}": { parameters: [tenant, id], ...p("Job status", "get", null, "Generic") },
    "/v1/{tenant}/edges/{id}": { parameters: [tenant, id], ...p("Remove an edge (extension x-unlink)", "delete", null, "Generic") },
  },
};
out["schema/openapi.json"] = openapi;
out["schema/mcp-tools.json"] = { note: "MCP tool definitions by toolset (orp serve --tools safe|full|mcp-memory)", tools: TOOLS };

let drift = 0;
for (const [path, obj] of Object.entries(out)) {
  const text = JSON.stringify(obj, null, 1) + "\n";
  if (process.argv.includes("--check")) {
    if (!existsSync(path) || readFileSync(path, "utf8") !== text) { console.error(`schema drift: ${path}`); drift++; }
  } else {
    mkdirSync(path.split("/").slice(0, -1).join("/"), { recursive: true });
    writeFileSync(path, text);
  }
}
if (drift) process.exit(1);
console.log(process.argv.includes("--check") ? "schema pack up to date" : `wrote ${Object.keys(out).length} files`);
