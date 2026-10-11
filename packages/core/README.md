# @orp/core

The ORP engine: admissibility-first recall over a single SQLite file, with zero runtime dependencies (SQLite comes
from Node's built-in `node:sqlite`, Node 22.13 or later).

```ts
import { Service, SqliteStore, Principal, loadEncoder, chooseRanker } from "@orp/core";
const enc = loadEncoder("hashing");
const svc = new Service(enc, { params: chooseRanker("auto", enc.name).params, store: new SqliteStore("orp.db") });
const me = new Principal("alice", ["sales"], "acme");
await svc.remember({ memories: [{ content: "Renewal is due in March", readers: ["group:sales"] }] }, me);
const pack = await svc.recall({ query: "when is the renewal?" }, me);
```

Embedded use is for local agents and desktop apps; for anything with more than one user, run `orp serve` and use a
client, so identity comes from credentials. The engine is a faithful port of the Python reference engine
(`orp/engine.py`) with per-tenant partitions and an incremental index; see results/PRODUCT_PARITY.md for how parity is
checked.
