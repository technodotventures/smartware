# Running orp in production

## Shape
One process, one SQLite file, any number of tenants. Each tenant's index lives in memory once used (built in the
background at start), and writes go to the file incrementally in one transaction per call. Put TLS in front with
any reverse proxy (Caddy, nginx, a cloud load balancer) and keep `orp` on 127.0.0.1 or a private network.

```bash
orp serve --http 8787 --host 127.0.0.1 --store /var/lib/orp/orp.db \
  --keys /etc/orp/keys.json \
  --oidc-issuer https://idp.example.com --oidc-audience https://memory.example.com/mcp --public-url https://memory.example.com \
  --encoder sif:/var/lib/orp/sif-ppmi-256.orpenc --tools safe --rate 50:100
```

## Authentication
- **API keys** (`orp keys add`): the file stores only SHA-256 hashes, is written with mode 0600, and is re-read when
  it changes (no restart to add or revoke). Give each agent, connector and service its own key. Connector keys take
  `--write-scope <prefix>` so they can only create records under their own scope; the server enforces it.
- **OIDC / OAuth 2.1**: the server is an OAuth resource server. Tokens must be signed (RS256, PS256, ES256 or EdDSA)
  by the issuer's JWKS, name this server as audience, and be in date. Claims map to a principal: `sub` (id), `groups`,
  `tenant` (configurable with `--principal-claim`, `--groups-claim`, `--tenant-claim`), and optional
  `orp_write_scopes`. A group named `*` in a token is ignored: superuser access comes only from an API key.
  `--require-scopes` enforces `orp:read` / `orp:write`. MCP clients discover the issuer from
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
- `/healthz`, `/readyz`; Prometheus metrics at `/metrics` when `ORP_METRICS_TOKEN` is set (send it as a bearer).

## Encoders and rankers
- `hashing` (default): no model, no network, deterministic. Weak on meaning; pairs with the `orp-rrf-1` ranker.
- `sif:<file.orpenc>`: the benchmarked encoder; `--ranker auto` then uses the learned `orp-linear-1` it was fitted
  with. Check the training data's licence before shipping this file in a product (it was fitted on benchmark text).
- `openai:<model>:<dim>[:<url>]`: any OpenAI-compatible embeddings API; key from `ORP_EMBED_KEY`. Vectors are cached
  in the store so restarts do not re-embed. The learned ranker was not fitted for it, so `auto` picks `orp-rrf-1`;
  measure with `orp eval` before switching.
- Changing the encoder of an existing store rebuilds each tenant's index on next use.

## Backups and moving data
- `orp backup --store orp.db --out orp-2026-10-11.db`: SQLite online backup, consistent while serving.
- Bundles (`GET /v1/<tenant>/export`, `POST /v1/<tenant>/import` as a tenant admin) move one tenant between engines
  losslessly (M62). The Python oracle reads and writes the same store file (one writer at a time).

## Sizing
See [results/PERFORMANCE.md](../results/PERFORMANCE.md). Up to about 10,000 units per tenant, recall stays under about
100 ms on two cores. Split very large tenants by scope.

## mem0-compatible REST
`/mem0/v1/memories/` (add, list, delete all), `/mem0/v1/memories/{id}/` (get, update, delete),
`/mem0/v1/memories/{id}/history/`, `/mem0/v1/memories/search/` and `/mem0/v2/memories/search/`. Send
`Authorization: Token <orp key>`. `user_id`, `agent_id` and `run_id` are scope, never identity: a key cannot read
another key's memories by naming their user. Fact extraction (`infer`) is not supported and is reported in
`degraded`.
