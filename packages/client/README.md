# @orp/client

A typed HTTP client for ORP memory. Zero dependencies, Node 22.13 or later (it uses the global `fetch`, so it also
runs in browsers and edge runtimes).

It is built so the safe thing is the easy thing:

- **Identity is bound, never passed.** A client is made from credentials. No verb takes a principal, tenant, actor or
  "on behalf of" argument; the gateway takes all of that from the bearer token (M56).
- **Writes fail closed.** `remember` writes for you alone (`readers: ["principal:<you>"]`) unless you pass explicit
  `readers` or `audience: "tenant"`. Widening an existing memory is an explicit `share()` call. Procedures (standing
  rules) go in as drafts that need approval unless you set `mode`.
- **One view per run.** `client.run()` pins `as_of` once and derives idempotency keys from the run and step, so a
  long run reasons over one consistent view and a retried or restarted run replays instead of writing twice
  (M17, M20, M52).
- **Nothing is swallowed.** Every failure is a typed `OrpError`. Any `degraded[]` on a response goes to `onDegraded`
  (by default a `console.warn` once per distinct value, never with memory content).
- **Retries are safe.** 429, 503 and network errors are retried with jittered backoff, but only for reads and for
  writes that carry an idempotency key. A plain write is never sent twice.

## Setup

```js
import { OrpClient, OrpError } from "@orp/client";

const memory = new OrpClient({
  url: "http://127.0.0.1:7477",
  token: process.env.ORP_TOKEN,      // or getToken: async () => currentAccessToken()
  // tenant: "acme",                 // optional: asked of the gateway once and cached
  // onDegraded: (values, { source }) => metrics.increment("orp.degraded", { source }),
  // retries: 2, retryDelayMs: 200, timeoutMs: 30000, fetch: customFetch,
});
```

Tokens come only from `token` or `getToken`. The client never logs them, and they never appear in error messages.

## Everyday verbs

```js
// a budgeted, cited context pack
const pack = await memory.recall("what does Alice drink?", { budgetTokens: 1500 });
for (const item of pack.items) console.log(item.text, item.citations);

// remember: a string, a MemoryInput, or an array of either
await memory.remember("Alice prefers oat milk flat whites", { kind: "preference" });
await memory.remember({ content: "Alice lives in Dubai", claim: { subject: "alice", predicate: "lives_in", object: "Dubai" } });
await memory.remember("The office closes at 6pm", { audience: "tenant" });          // everyone in the tenant
await memory.remember({ content: "Q3 targets", readers: ["group:sales"] });         // an explicit audience

// widen an existing memory (reads it, then upserts with if_version)
await memory.share(id, ["principal:bob"]);

// forget: retract (default), redact or purge; dryRun changes nothing
const preview = await memory.forget(id, { dryRun: true });
await memory.forget(id, { reason: "user asked" });

// always-loaded context: a budget is required
const profile = await memory.profile({ budgetTokens: 400 });

// the version chain of a claim
const h = await memory.history("alice", "lives_in");
```

`readers: null` is refused on purpose: it means everyone in the tenant, which you say with `audience: "tenant"`.

## Agent runs

```js
const run = memory.run({ runId: job.id });   // as_of resolved now, as an ISO string
await run.recall("open questions");          // sends as_of = run.asOf
await run.remember("decided to ship Friday"); // idempotency_key run:<runId>:1
await run.remember("told marketing");          // run:<runId>:2
await run.remember("final note", { step: "summary" }); // run:<runId>:summary
```

If the process dies and the job restarts with the same `runId`, the same steps replay: the gateway returns the
original ids with `replayed: true`. Steps count from 1 per run client; give writes whose order can change an
explicit `step`. Passing a different `asOf` to a run's recall is an error: start another run instead. `forget` has
no idempotency key in the protocol, so it is never retried.

**Coarse times.** An `asOf` or `knownAt` given as a date means the end of that period: `"2024"` is the end of 2024,
`"2024-05"` the end of May and `"2024-05-12"` the end of that day (`asOfTime()` shows the conversion). Without this, a
question "as of 12 May" would cut off at midnight and miss everything said that day.

## Acting for users

A platform holds a service credential and makes a per-user client from that user's bearer, typically an OIDC access
token that the gateway verifies against your identity provider:

```js
const asUser = memory.forUser(req.headers.authorization.slice(7));
await asUser.remember("prefers morning standups");   // readers: [principal:<that user>]
```

The service client asserts nothing about the user. The gateway maps the token's claims to a principal and groups.

## Advanced verbs

`memory.advanced` has the rest: `get`, `getMany`, `list`, `pages` and `all` (async iterators over list pages and
records), `neighbours`, `thread`, `link`, `unlink`, `edges`, `feedback`, `approve`, `reflect`, `job`, `changesPage`,
`changes` (an async iterator that follows the change feed, and keeps polling with `follow: true` until its
`signal` aborts), `export`, `import`, `capabilities`, `whoami` and `audit`.

```js
for await (const change of memory.advanced.changes({ cursor: saved, follow: true, signal })) handle(change);
```

## Errors

`OrpError` has `code` (`unsupported_parameter`, `invalid_parameter`, `version_conflict`, `forbidden`,
`not_found`, `budget_too_small`, `rate_limited`, `unauthorized`, `payload_too_large`, plus `unavailable` and
`network` from the transport), `status` (0 when no response arrived), `details` and `requestId`.

## Tenant discovery

When `tenant` is not given the client asks the gateway who the token is. The current gateway checks the tenant in
the path before it answers `whoami`, so the client first tries `default` (or, for `forUser` clients, the parent's
tenant), then falls back to reading the tenant from the MCP endpoint's capabilities resource. Pass `tenant` to skip
both.

## Types

The request and response types (`MemoryInput`, `Memory`, `ContextPack`, `ContextItem`, `RecallOptions`, and so on)
are written by hand to match `schema/orp-0.1`. A test sends every option the client supports and checks that each
key in the `MemoryInput`, `RememberRequest`, `RecallRequest`, `ForgetRequest` and `ProfileRequest` bodies is a
property of the schema.

## Tests

```sh
node --test packages/client/test/*.test.js
```

Each test file starts its own in-process gateway on port 0.
