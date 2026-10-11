// Shared test helper: an in-process ORP server on port 0 with its own scratch store and keys.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { HashingEncoder, Service, SqliteStore, chooseRanker } from "@orp/core";
import { Authenticator, Gateway, KeyStore, Log } from "@orp/server";
import { slackPrincipalResolver, withMembership } from "../dist/index.js";

export const TENANT = "acme";

/** Start a server. With membership, the gateway adds channel:<id> groups from a connector's membership file. */
export async function startServer({ membership } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "orp-conn-"));
  const keysPath = join(dir, "keys.json");
  const ks = new KeyStore(keysPath);
  const tokens = {
    connector: ks.add({ id: "connector-bot", tenant: TENANT }),
    alice: ks.add({ id: "alice", tenant: TENANT }),
    bob: ks.add({ id: "bob", tenant: TENANT }),
    carol: ks.add({ id: "carol", tenant: TENANT }),
    admin: ks.add({ id: "root", tenant: TENANT, groups: ["*"] }),
  };
  const enc = new HashingEncoder();
  const { params } = chooseRanker("auto", enc.name);
  const svc = new Service(enc, { params, store: new SqliteStore(join(dir, "orp.db")) });
  let auth = new Authenticator(new KeyStore(keysPath), null);
  const membershipPath = join(dir, "membership.json");
  if (membership) auth = withMembership(auth, slackPrincipalResolver({ membershipPath, connector: membership, tenant: TENANT }));
  const gw = new Gateway(svc, auth, { host: "127.0.0.1", port: 0, mcp: { toolsets: ["safe"], defaultReaders: "self" } }, new Log("silent"));
  await gw.listen();
  return {
    url: gw.url, tenant: TENANT, tokens, dir, membershipPath, svc,
    async close() { await gw.close(); svc.close(); },
  };
}

export async function api(srv, who, method, path, body) {
  const r = await fetch(`${srv.url}/v1/${srv.tenant}${path}`, {
    method, headers: { authorization: `Bearer ${srv.tokens[who]}`, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let json; try { json = JSON.parse(text); } catch { json = text; }
  return { status: r.status, body: json };
}

export const recallIds = async (srv, who, query) =>
  (await api(srv, who, "POST", "/recall", { query })).body.items.flatMap((i) => i.memory_ids);

export const getRec = async (srv, who, id) => {
  const r = await api(srv, who, "GET", `/memories/${encodeURIComponent(id)}`);
  return r.status === 200 ? r.body : null;
};
