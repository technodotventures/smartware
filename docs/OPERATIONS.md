# Running smartware in production

## Shape
One process, one SQLite file, any number of tenants (or several processes on Postgres, below). Each tenant's index lives in memory once used (built in the
background at start), and writes go to the file incrementally in one transaction per call. Put TLS in front with
any reverse proxy (Caddy, nginx, a cloud load balancer) and keep `smartware` on 127.0.0.1 or a private network.

```bash
smartware serve --http 8787 --host 127.0.0.1 --store /var/lib/smartware/smartware.db \
  --keys /etc/smartware/keys.json \
  --oidc-issuer https://idp.example.com --oidc-audience https://memory.example.com/mcp --public-url https://memory.example.com \
  --encoder sif:/var/lib/smartware/sif-ppmi-256.swenc --tools safe --rate 50:100
```

## Postgres, and several nodes
For a hosted service, point `--store` at Postgres (14 or later; tested on 16). The tables (prefix `smartware_`, schema
`public` or `?schema=<name>`) are created on first start. Put the password in `PGPASSWORD`, not the URL, and use
`sslmode=verify-full` (with `sslrootcert=<ca.pem>` for a private CA) outside a private network.

```bash
PGPASSWORD=... smartware serve --http 8787 --host 0.0.0.0 --keys /etc/smartware/keys.json \
  --store "postgres://smartware@db.internal:5432/smartware?sslmode=verify-full&sslrootcert=/etc/smartware/db-ca.pem" \
  --node-url http://10.0.1.17:8787       # the address other smartware nodes reach this one on
```

Any number of nodes can share the database. Each tenant is served by one node at a time: the node that first gets a
request for it takes the tenant's lease (30 s, renewed every 10 s; `--lease-s`), loads it and keeps it in memory;
another node that gets a request for that tenant forwards it there (`--node-url`) and passes the answer back. Every
write commits only if the writing node still holds the lease (checked in the same transaction), so a node that
stalls past its lease cannot overwrite the node that took over; it answers 503 with `retry-after` and lets go. A
node releases tenants it has not served for 10 minutes (`--idle-tenant-s`), and all of them on shutdown, so
tenants spread across nodes as traffic comes in. Without `--node-url`, other nodes answer 503 for tenants this node
holds: route by tenant at the load balancer instead (any stable hash of the tenant). MCP sessions live on one node;
a client whose session moves gets 404 and starts a new session, as the MCP specification provides. Keys files and
OIDC settings must be the same on every node: a forwarded request is authenticated again by the node that serves it.
Give each node its own `--node-url` (each process also gets a random suffix on its node id, so two processes never
share a lease); a node restarted at the same URL takes its old tenants back at once instead of waiting for the leases
to run out. Keep node URLs on a private network: forwarding goes only to the URL a node registered, with the
request's path and query.

Backups: `smartware backup --store postgres://... --out smartware-2026-10-11.db` takes a consistent snapshot into a SQLite file
that any `smartware` (or the Python oracle) can open; for large stores use your Postgres backups (`pg_dump`, PITR) as
well. Restoring one tenant elsewhere: `smartware export` / `smartware import` (below).

## Authentication
- **API keys** (`smartware keys add`): the file stores only SHA-256 hashes, is written with mode 0600, and is re-read when
  it changes (no restart to add or revoke). Give each agent, connector and service its own key. Connector keys take
  `--write-scope <prefix>` so they can only create records under their own scope; the server enforces it.
- **OIDC / OAuth 2.1**: the server is an OAuth resource server. Tokens must be signed (RS256, PS256, ES256 or EdDSA)
  by the issuer's JWKS, name this server as audience, and be in date. Claims map to a principal: `sub` (id), `groups`,
  `tenant` (configurable with `--principal-claim`, `--groups-claim`, `--tenant-claim`), and optional
  `smartware_write_scopes`. A group named `*` in a token is ignored: superuser access comes only from an API key.
  `--require-scopes` enforces `smartware:read` / `smartware:write`. MCP clients discover the issuer from
  `/.well-known/oauth-protected-resource`.
- **Groups from connectors**: `--membership <file>` adds `channel:<id>` groups from the Slack connector's membership
  file, so a user who leaves a channel loses access to its messages on the next sync.
- `--dev` (no authentication) is refused on anything but localhost.

## Limits and safety
- Rate limit per principal (`--rate rps:burst`, default 50:100), 1 MiB request bodies (64 MiB for import), 120 s
  request timeout, origin allowlist (`--allowed-origin`) for browsers and against DNS rebinding of MCP.
- Unknown fields are rejected, never ignored. Errors are `{error: {code, message, details}}`.
- Logs are JSON lines on stderr with request id, route, status, latency, tenant and principal id: never bodies,
  memory content or tokens.
- `/healthz`, `/readyz`; Prometheus metrics at `/metrics` when `SMARTWARE_METRICS_TOKEN` is set (send it as a bearer).

## Encoders and rankers
- `hashing` (default): no model, no network, deterministic. Weak on meaning; pairs with the `smartware-rrf-1` ranker.
- `sif:<file.swenc>`: the benchmarked encoder; `--ranker auto` then uses the learned `smartware-linear-1` it was fitted
  with. Check the training data's licence before shipping this file in a product (it was fitted on benchmark text).
- `openai:<model>:<dim>[:<url>]`: any OpenAI-compatible embeddings API; key from `SMARTWARE_EMBED_KEY`. Vectors are cached
  in the store so restarts do not re-embed. The learned ranker was not fitted for it, so `auto` picks `smartware-rrf-1`;
  measure with `smartware eval` before switching.
- Changing the encoder of an existing store rebuilds each tenant's index on next use.

## Backups and moving data
- `smartware backup --store smartware.db --out smartware-2026-10-11.db`: SQLite online backup, consistent while serving.
- Bundles move one tenant between engines losslessly (M62): `smartware export --store smartware.db --tenant acme --out acme.smartware
  [--render]` writes a zip (or a directory, without the `.smartware` suffix) with `manifest.json` (counts, sha256 of every
  file, capabilities), `memories.jsonl`, `edges.jsonl`, `audit.jsonl` and, with `--render`, a Markdown view per thread
  and entity. `smartware import acme.smartware --store other.db --tenant acme` checks the file hashes before writing anything (they catch a damaged or truncated file; they are not a signature, so import only bundles you trust). Over
  HTTP: `GET /v1/<tenant>/export`, `POST /v1/<tenant>/import` as a tenant admin.
- From other projects: `smartware import-from mcp-memory memory.json|mem0 export.json|letta agent.af --tenant acme --as
  alice [--readers tenant]`. Records belong to `--as` and are readable by them only unless `--readers` widens it;
  running it again writes nothing new, and anything forgotten since stays forgotten.
- The Python oracle reads and writes the same store file (one writer at a time).

## Sizing
See [results/PERFORMANCE.md](../results/PERFORMANCE.md). Up to about 10,000 units per tenant, recall stays under about
100 ms on two cores. Split very large tenants by scope.

## mem0-compatible REST
`/mem0/v1/memories/` (add, list, delete all), `/mem0/v1/memories/{id}/` (get, update, delete),
`/mem0/v1/memories/{id}/history/`, `/mem0/v1/memories/search/` and `/mem0/v2/memories/search/`. Send
`Authorization: Token <smartware key>`. `user_id`, `agent_id` and `run_id` are scope, never identity: a key cannot read
another key's memories by naming their user. Fact extraction (`infer`) is not supported and is reported in
`degraded`.
