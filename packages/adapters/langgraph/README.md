# @orp/langgraph

A LangGraph JS `BaseStore`-compatible store backed by ORP, for long-term memory in LangGraph graphs.

## Host interface targeted

Written from my knowledge of `BaseStore` in `@langchain/langgraph-checkpoint` (used by `@langchain/langgraph`
0.2 to 0.4, 2025). **Check it against the release you run.** The methods:

```ts
batch(ops) · get(namespace, key) · search(namespacePrefix, { query, filter, limit, offset })
put(namespace, key, value, index?) · delete(namespace, key) · listNamespaces({ prefix, suffix, maxDepth, limit, offset })
start() · stop()
```

`OrpStore` implements these structurally and does not extend LangGraph's class. If your release checks
`instanceof BaseStore`, wrap it:

```js
import { BaseStore } from "@langchain/langgraph";
class MemoryStore extends BaseStore {
  constructor(inner) { super(); this.inner = inner; }
  batch(ops) { return this.inner.batch(ops); }
}
```

## Setup

```js
import { OrpClient } from "@orp/client";
import { OrpStore } from "@orp/langgraph";

const memory = new OrpClient({ url: process.env.ORP_URL, token: process.env.ORP_TOKEN });
const store = new OrpStore(memory, { prefix: "langgraph", budgetTokens: 4000 /*, asOf, audience, index */ });
const graph = builder.compile({ checkpointer, store });

// inside a node
await store.put(["users", userId, "prefs"], "drink", { drink: "flat white" });
const hits = await store.search(["users", userId], { query: "what does the user drink?" });
```

## Mapping

| LangGraph | ORP |
|---|---|
| namespace `["users", "u1", "prefs"]` | scope `langgraph/users/u1/prefs` (each segment percent-encoded) |
| key | client id `kv:<owner>:<prefix>:<namespace...>:<key>`; the owner is your principal (or `_tenant` with `audience: "tenant"`), so keys never collide across principals |
| value | `meta.kv.value`, returned exactly as stored; the memory's content is a text rendering (the `index` fields, or the JSON) for recall |
| `search` with a query | recall within the namespace scope, then the records by id; `score` is the pack score |
| `search` without a query | the list extension within the scope, newest first |
| `put(..., index: false)` | stored with kind `x-kv-noindex`: listed, never returned by a query search |
| `delete`, `put(..., null)` | forget with `mode: "purge"`; a later put of the same key works |
| `listNamespaces` | distinct namespaces under the prefix scope, sorted |

A host `user_id` belongs in the namespace, where it becomes a scope segment. It never becomes the principal: the
principal is the client's credentials, and the audience is the store's `audience` (`"self"` by default).

## ORP level and what it loses

**L2.** The `BaseStore` interface has no place for `as_of`, budgets or principals, so:

- `as_of` and the recall budget come from the store's config, the same for every call. The store reports
  `langgraph:as_of-from-config` and `langgraph:budget-from-config` through the client's `onDegraded` the first time
  it searches. With `asOf` set, a search without a query cannot apply it (the list route has no clock over HTTP) and
  the store reports `langgraph:no-as_of-on-list`.
- `filter` is applied after recall's top-k cut, so a filtered query search can return fewer than `limit` items. It
  over-fetches threefold and reports `langgraph:filter-after-recall`.
- Readers per item cannot be expressed: the audience is store-wide config.
- `listNamespaces` and searches without a query read every record under the scope, so they cost O(records).
- Values that look like credentials are labelled secret by the engine and then read back as missing. Do not keep
  secrets in the store.

`OrpStore.capabilities` lists the same, and `conformance.json` lists the `orp doctor` checks this interface can
exercise.
