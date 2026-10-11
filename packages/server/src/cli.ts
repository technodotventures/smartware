// orp: one binary for the kit.
//   orp serve                MCP over stdio (default), for any MCP client on this machine
//   orp serve --http 8787    HTTP binding + MCP Streamable HTTP (OAuth resource server) on one port
//   orp keys add|list|revoke|migrate
//   orp backup --store <db> --out <file>
//   orp doctor <url> ...     conformance and integration checks against any endpoint
//   orp eval ...             re-measure recall on your own data with your own reader
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { existsSync } from "node:fs";
import { chooseRanker, loadEncoder, Principal, Service, SqliteStore } from "@orp/core";
import { Authenticator, JwtVerifier, KeyStore } from "./auth.js";
import { Gateway } from "./http.js";
import { McpSession, stdioPrincipal } from "./mcp.js";
import { Log } from "./log.js";

export const VERSION = "0.2.0";

type Flags = Record<string, string | boolean | string[]>;

export function parseFlags(argv: string[]): { pos: string[]; f: Flags } {
  const pos: string[] = [];
  const f: Flags = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]!;
    if (!a.startsWith("--")) { pos.push(a); continue; }
    const [k, v] = a.slice(2).split("=", 2) as [string, string | undefined];
    const next = argv[i + 1];
    const val: string | boolean = v !== undefined ? v : next !== undefined && !next.startsWith("--") ? (i++, next) : true;
    const prev = f[k];
    f[k] = prev === undefined ? val : ([] as string[]).concat(prev as any, val as any);
  }
  return { pos, f };
}

const s = (f: Flags, k: string, d?: string): string | undefined => (typeof f[k] === "string" ? (f[k] as string) : d);
const list = (f: Flags, k: string): string[] => ([] as any[]).concat(f[k] ?? []).flatMap((x: any) => String(x).split(",")).filter(Boolean);

function defaultStore(): string {
  return process.env.ORP_STORE ?? join(homedir(), ".orp", "orp.db");
}

export function buildService(f: Flags, log: Log): Service {
  const encSpec = s(f, "encoder", process.env.ORP_ENCODER ?? "hashing");
  const enc = loadEncoder(encSpec);
  const { name, params, notes } = chooseRanker(s(f, "ranker", process.env.ORP_RANKER ?? "auto"), enc.name);
  for (const n of notes) log.warn(n);
  const storePath = f["memory"] === true ? null : resolve(s(f, "store", defaultStore())!);
  const store = storePath ? new SqliteStore(storePath) : null;
  return new Service(enc, { params, store, rankerName: name, notes, secretScan: f["no-secret-scan"] !== true });
}

async function serveStdio(svc: Service, f: Flags, log: Log) {
  const pr = stdioPrincipal(process.env);
  const out = (m: unknown) => process.stdout.write(JSON.stringify(m) + "\n");
  const pending = new Map<number, (r: any) => void>();
  let nextId = 1_000_000;
  const session = new McpSession(svc, pr, {
    toolsets: list(f, "tools").length ? list(f, "tools") : ["safe"],
    defaultReaders: (s(f, "default-readers", "self") as any) === "tenant" ? "tenant" : s(f, "default-readers", "self") === "self" ? "self" : list(f, "default-readers"),
    notify: out,
    elicit: (message) => new Promise((ok) => {
      const id = nextId++;
      pending.set(id, (r) => ok(Boolean(r?.action === "accept" && r?.content?.confirm)));
      out({ jsonrpc: "2.0", id, method: "elicitation/create", params: { message,
        requestedSchema: { type: "object", properties: { confirm: { type: "boolean" } }, required: ["confirm"] } } });
    }),
  });
  await session.primeCursor();
  const queue: any[] = [];
  let busy = false;
  const pump = async () => {
    if (busy) return;
    busy = true;
    while (queue.length) {
      const msg = queue.shift();
      const res = await session.handle(msg);
      if (res) out(res);
    }
    busy = false;
  };
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on("line", (line) => {
    if (!line.trim()) return;
    let msg: any;
    try { msg = JSON.parse(line); } catch { out({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } }); return; }
    if (msg && msg.method === undefined && pending.has(msg.id)) { pending.get(msg.id)!(msg.result); pending.delete(msg.id); return; }
    queue.push(msg);
    void pump();
  });
  rl.on("close", () => { svc.close(); process.exit(0); });
  log.info("orp mcp stdio ready", { tenant: pr.tenant, principal: pr.id, store: svc.store?.path ?? "(memory)" });
}

async function serveHttp(svc: Service, f: Flags, log: Log) {
  const keys = new KeyStore(s(f, "keys", process.env.ORP_KEYS) ?? null);
  for (const w of keys.warnings) log.warn(w);
  const issuer = s(f, "oidc-issuer", process.env.ORP_OIDC_ISSUER);
  const port = parseInt(s(f, "http", s(f, "port", "8787"))!, 10);
  const host = s(f, "host", "127.0.0.1")!;
  const publicUrl = s(f, "public-url", process.env.ORP_PUBLIC_URL);
  const jwt = issuer ? new JwtVerifier({ issuer, audience: list(f, "oidc-audience").length ? list(f, "oidc-audience") : [`${publicUrl ?? `http://${host}:${port}`}/mcp`],
    jwks: s(f, "jwks"), principalClaim: s(f, "principal-claim"), groupsClaim: s(f, "groups-claim"), tenantClaim: s(f, "tenant-claim"),
    defaultTenant: s(f, "default-tenant"), requiredScopes: f["require-scopes"] ? { read: "orp:read", write: "orp:write" } : undefined }) : null;
  let dev: Principal | null = null;
  if (f["dev"]) {
    if (host !== "127.0.0.1" && host !== "localhost" && host !== "::1") throw new Error("--dev (no authentication) is only allowed on localhost");
    dev = stdioPrincipal(process.env);
    log.warn("dev mode: requests without a token act as the local principal", { principal: dev.id, tenant: dev.tenant });
  }
  if (!keys.size && !jwt && !dev) throw new Error("no way to authenticate: pass --keys <file>, --oidc-issuer <url>, or --dev on localhost");
  const rate = s(f, "rate", "50:100")!.split(":").map(Number);
  const gw = new Gateway(svc, new Authenticator(keys, jwt, dev), {
    host, port, publicUrl, authorizationServers: issuer ? [issuer] : [], rate: { rps: rate[0]!, burst: rate[1]! },
    allowedOrigins: list(f, "allowed-origin"), metricsToken: process.env[s(f, "metrics-token-env", "ORP_METRICS_TOKEN")!],
    mcp: { toolsets: list(f, "tools").length ? list(f, "tools") : ["safe"],
      defaultReaders: (s(f, "default-readers", "self") === "tenant" ? "tenant" : s(f, "default-readers", "self") === "self" ? "self" : list(f, "default-readers")) as any },
  }, log);
  await gw.listen();
  log.info("orp http ready", { url: gw.url, mcp: `${gw.url}/mcp`, store: svc.store?.path ?? "(memory)", auth: [keys.size ? "keys" : "", jwt ? "oidc" : "", dev ? "dev" : ""].filter(Boolean) });
  const stop = async () => { log.info("shutting down"); await gw.close(); svc.close(); process.exit(0); };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}

const HELP = `orp ${VERSION}: the ORP memory kit

  orp serve [--store <db>] [--encoder hashing|sif:<file.orpenc>|openai:<model>:<dim>] [--ranker auto|orp-linear-1|orp-rrf-1]
            [--tools safe|full|mcp-memory,...] [--default-readers self|tenant|<tokens>]
      MCP over stdio. Principal from ORP_PRINCIPAL / ORP_GROUPS / ORP_TENANT.

  orp serve --http <port> [--host 127.0.0.1] [--keys <keys.json>] [--oidc-issuer <url> --oidc-audience <aud> --jwks <url|file>]
            [--public-url <url>] [--allowed-origin <origin>] [--rate rps:burst] [--dev]
      ORP HTTP binding at /v1/<tenant>/... and MCP Streamable HTTP at /mcp, on one port.

  orp keys add --keys <keys.json> --id <principal> --tenant <tenant> [--groups a,b] [--label <name>]
  orp keys list|revoke|migrate --keys <keys.json> [--id <principal>]
  orp backup [--store <db>] --out <file>
  orp doctor <url> --token-env <ENV> [--level L1..L4] [--principals <file.json>]
  orp eval --data <questions.jsonl> [--reader ...]          (see docs/EVAL.md)
`;

export async function main(argv: string[]) {
  const { pos, f } = parseFlags(argv);
  const cmd = pos[0];
  const log = new Log((s(f, "log-level", process.env.ORP_LOG ?? "info") as any));
  if (!cmd || cmd === "help" || f.help) { process.stdout.write(HELP); return; }
  if (cmd === "version" || f.version) { process.stdout.write(VERSION + "\n"); return; }
  if (cmd === "serve") {
    const svc = buildService(f, log);
    if (f.http !== undefined || f.port !== undefined) await serveHttp(svc, f, log);
    else await serveStdio(svc, f, log);
    return;
  }
  if (cmd === "keys") {
    const path = s(f, "keys", process.env.ORP_KEYS);
    if (!path) throw new Error("--keys <file> is required");
    const ks = new KeyStore(path);
    const sub = pos[1];
    if (sub === "add") {
      const id = s(f, "id");
      if (!id) throw new Error("--id is required");
      const token = ks.add({ id, tenant: s(f, "tenant", "default"), groups: list(f, "groups"), label: s(f, "label") });
      process.stdout.write(`${token}\n`);
      process.stderr.write("This token is shown once. Only its hash is stored. Keep it in a secret manager, not in code.\n");
    } else if (sub === "list") {
      for (const k of ks.list()) process.stdout.write(`${k.id}\t${k.tenant}\t${(k.groups ?? []).join(",")}\t${k.label ?? ""}\t${k.created ?? ""}\n`);
    } else if (sub === "revoke") {
      const id = s(f, "id");
      if (!id) throw new Error("--id is required");
      process.stdout.write(`revoked ${ks.revoke(id, s(f, "label"))}\n`);
    } else if (sub === "migrate") {
      ks.save();
      process.stdout.write(`${path} now stores only token hashes\n`);
    } else throw new Error("keys: add | list | revoke | migrate");
    return;
  }
  if (cmd === "backup") {
    const out = s(f, "out");
    if (!out) throw new Error("--out is required");
    const storePath = resolve(s(f, "store", defaultStore())!);
    if (!existsSync(storePath)) throw new Error(`${storePath} does not exist`);
    const svc = new Service(loadEncoder("hashing"), { store: new SqliteStore(storePath) });
    await svc.saveTo(resolve(out));
    svc.close();
    process.stdout.write(`backed up ${storePath} to ${resolve(out)}\n`);
    return;
  }
  if (cmd === "doctor") { const { doctor } = await import("./doctor.js"); process.exitCode = await doctor(pos.slice(1), f); return; }
  if (cmd === "eval") { const { evalCmd } = await import("./eval.js"); process.exitCode = await evalCmd(pos.slice(1), f); return; }
  throw new Error(`unknown command ${cmd}; try orp help`);
}
