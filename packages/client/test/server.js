// Test support: an in-process Smartware gateway on port 0 with an in-memory engine, API keys for a few principals and an
// OIDC issuer (a local JWKS file) for forUser tests. Shared by the client and adapter tests.
import { generateKeyPairSync, sign } from "node:crypto";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { HashingEncoder, Service, chooseRanker } from "@technodotventures/smartware-core";
import { Authenticator, Gateway, JwtVerifier, KeyStore, Log } from "smartware";

export const ISSUER = "https://idp.example.test";
export const AUDIENCE = "smartware-test";

export async function startServer({ tenant = "acme", rate } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "smartware-client-"));
  const ks = new KeyStore(join(dir, "keys.json"));
  const tokens = {
    alice: ks.add({ id: "alice", tenant, groups: ["sales"] }),
    bob: ks.add({ id: "bob", tenant, groups: ["eng"] }),
    admin: ks.add({ id: "root", tenant, groups: ["admin"] }),
    platform: ks.add({ id: "platform-svc", tenant, groups: [] }),
    outsider: ks.add({ id: "olga", tenant: "elsewhere", groups: [] }),
  };
  const { publicKey, privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = { ...publicKey.export({ format: "jwk" }), kid: "k1", use: "sig", alg: "RS256" };
  writeFileSync(join(dir, "jwks.json"), JSON.stringify({ keys: [jwk] }));
  const jwtv = new JwtVerifier({ issuer: ISSUER, audience: AUDIENCE, jwks: join(dir, "jwks.json"), tenantClaim: "org" });
  const enc = new HashingEncoder();
  const svc = new Service(enc, { params: chooseRanker("auto", enc.name).params, store: null });
  const gw = new Gateway(svc, new Authenticator(new KeyStore(join(dir, "keys.json")), jwtv), {
    host: "127.0.0.1", port: 0, ...(rate ? { rate } : {}), mcp: { toolsets: ["safe"], defaultReaders: "self" },
  }, new Log("silent"));
  await gw.listen();
  const jwt = (claims) => {
    const now = Math.floor(Date.now() / 1000);
    const full = { iss: ISSUER, aud: AUDIENCE, iat: now, exp: now + 600, org: tenant, ...claims };
    const h = Buffer.from(JSON.stringify({ alg: "RS256", kid: "k1", typ: "JWT" })).toString("base64url");
    const p = Buffer.from(JSON.stringify(full)).toString("base64url");
    return `${h}.${p}.${sign("sha256", Buffer.from(`${h}.${p}`), privateKey).toString("base64url")}`;
  };
  return {
    url: gw.url, tokens, tenant, svc, jwt,
    async close() { await gw.close(); svc.close(); rmSync(dir, { recursive: true, force: true }); },
  };
}

/** A fetch wrapper that records request bodies and can script failures. */
export function recordingFetch(script = () => undefined) {
  const calls = [];
  const f = async (url, init) => {
    const u = new URL(url);
    const verb = u.pathname.split("/").slice(3).join("/");
    const body = init.body ? JSON.parse(init.body) : undefined;
    const call = { method: init.method, verb, body, url: u };
    calls.push(call);
    const action = script(call, calls);
    if (action === "429") return new Response(JSON.stringify({ error: { code: "rate_limited", message: "slow down", details: {} } }), { status: 429 });
    if (action === "throw-before") throw new TypeError("fetch failed");
    const res = await fetch(url, init);
    if (action === "503-after") { await res.text(); return new Response(JSON.stringify({ error: { code: "unavailable", message: "lost", details: {} } }), { status: 503 }); }
    if (action === "throw-after") { await res.text(); throw new TypeError("fetch failed"); }
    return res;
  };
  return { fetch: f, calls };
}
