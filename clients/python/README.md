# orp-client (Python)

A Python client for the ORP memory protocol over its HTTP binding (spec 0.1), plus compatibility shims for
LangGraph, mem0 and Letta. Standard library only, Python 3.10 or later, no dependencies.

"ORP" is a working name; the package and module names will be renamed later.

```python
from orp_client import OrpClient

client = OrpClient("https://memory.example.com", token_env="ORP_TOKEN")

client.remember("Priya is vegetarian and allergic to peanuts")   # readable by you alone
pack = client.recall("what can Priya eat?", budget_tokens=800)    # budgeted, cited context
for item in pack.items:
    print(item.score, item.text, item.citations)
```

## Install

```
pip install ./clients/python
```

There is nothing to compile and no install scripts.

## Credentials

Pass at most one of:

| Argument | Use it for |
|---|---|
| `token="..."` | a token you already hold in memory |
| `token_env="ORP_TOKEN"` | the **name** of an environment variable; it is read on every request, so a rotated token takes effect at once |
| `get_token=callable` | a function that returns a fresh token, for example from a secret manager or an OAuth refresh |

With none, requests carry no credentials, which only a `--dev` server on localhost accepts.

The token is sent only in the `Authorization` header. It is never logged, never put in an error message, and never
shown by `repr()`. The client refuses to send a token over plain `http://` to anything but a loopback address unless
you pass `allow_insecure_http=True`.

The tenant is optional. Leave it out and the client asks the server. (The HTTP binding's `whoami` sits under
`/v1/{tenant}/`, so it cannot answer that question; the client asks the MCP binding on the same server, whose
`resources/list` names the tenant. Passing `tenant=` skips this.)

## The everyday verbs

| Call | What it does |
|---|---|
| `recall(query, *, as_of, budget_tokens, kinds, include_history, mode, min_coverage, scope, ...)` | A context pack: items with text, score, citations and validity, inside a token budget (2000 unless you say otherwise) |
| `remember(content_or_memory_or_list, *, kind, readers, audience, claim, thread, idempotency_key, mode, ...)` | Store a string, a memory dict (`MemoryInput`) or a list of them |
| `forget(ids, *, mode="retract", dry_run=False, reason=None)` | Retract, redact or purge, with cascade to derived records |
| `profile(budget_tokens, subject=None)` | The always-loaded records (`load: "always"`) that fit the budget. The budget is required |
| `share(id, readers)` | Widen who can read a record: a list of access tokens, or `"tenant"` |
| `history(subject, predicate)` | Every version of a claim you can read |

Responses are typed (`ContextPack`, `RememberResult`, `ForgetResult`, `Profile`, `Memory`), and each keeps the
full server response in `.raw`.

Everything else lives under `client.advanced`: `get`, `get_many`, `list` and `iter_list` (pages of readable
records), `neighbours`, `thread`, `link`, `unlink`, `edges`, `feedback`, `approve`, `reflect`, `job`, `changes`
(an iterator that follows the cursor to the end of the feed; `feed.cursor` is where to resume), `export`, `import_`,
`capabilities` and `whoami`.

## Safe by construction

**Identity is bound, never passed.** No method takes a principal, tenant or actor argument. The server resolves
them from the credential. (`actor` inside a memory dict is a client assertion that the server records as such; the
authenticated writer is `written_by`.)

**Writes fail closed.** `remember` sets `readers` to `["principal:<you>"]` unless you pass `readers=[...]` or
`audience="tenant"`. `readers=None` is refused, because on the wire it means everyone in the tenant; say
`audience="tenant"` if that is what you mean. Widening later is an explicit `share()`, which reads the record and
writes it back with `if_version`, so a concurrent change is never overwritten. Drafts cannot be shared (an upsert
would activate them); approve them first.

**Standing rules wait for approval.** `kind="procedure"` defaults to `mode="propose"`, so the record is a draft
until someone approves it. A batch that mixes procedures with other kinds must name its mode.

**Unknown parameters are hard errors.** A memory dict with a field outside `MemoryInput` raises
`UnsupportedParameterError` before anything is sent. An unknown keyword argument is a Python `TypeError`. Values the
server does not support (a mode, a kind) come back as `UnsupportedParameterError` too.

**Nothing is swallowed.** Whenever a response carries a non-empty `degraded[]`, the client calls
`on_degraded(verb, degraded)`. The default logs a warning on the `orp_client` logger (codes only, never content).

**Typed errors.** Every protocol error code has its own class, all subclasses of `OrpError(code, message, status,
details)`: `UnsupportedParameterError`, `InvalidParameterError`, `VersionConflictError` (`details["current"]` is the
stored version), `ForbiddenError`, `NotFoundError`, `BudgetTooSmallError`, `RateLimitedError`, `UnauthorizedError`,
`PayloadTooLargeError`, plus `UnavailableError`, `InternalError` and `TransportError` (no usable response).
`NotFoundError` covers both "absent" and "present but not yours": the server makes them look the same.

### Runs

```python
run = client.run()              # or client.run(run_id="job-2026-10-11", as_of="2026-10-01T00:00:00Z")
run.recall("open tasks")        # every recall and profile uses the same pinned as_of
run.remember("step 1 done")     # idempotency key run:<run_id>:1
run.remember("published", step="publish")   # run:<run_id>:publish
```

A run-scoped client pins `as_of` once (now, unless you give it) so a long run reasons over one consistent view. Its
writes get idempotency keys `run:<run_id>:<step>`, where `step` counts up from 1 unless you name it. Replaying a run
with the same `run_id` replays its writes: the server returns the first write's ids with `replayed=True` and stores
nothing new. Records written after the pinned time, including the run's own writes, are not visible to the run's
recalls. That is the point of a consistent view.

`as_of` is pinned with the client's clock (UTC). If the client's clock runs behind the server's, records written
in the last moments before the run started may fall outside the view; pass `as_of` explicitly if that matters.
`get` and `list` are not pinned: the HTTP binding has no `as_of` on those routes.

A coarse `as_of` or `known_at` means the end of that period: `"2024"` is the end of 2024, `"2024-05"` the end of May
and `"2024-05-12"` (or a `date`) the end of that day (`as_of_time()` shows the conversion), so a question "as of
12 May" does not miss what was said later that day.

### Acting for end users

```python
user_client = service_client.for_user(signed_assertion)
```

A platform holding a service credential mints a per-user client from a signed assertion (an OIDC or OAuth JWT from
its identity provider). The server verifies the assertion and maps its claims to the user's principal, groups and
tenant. The new client carries only the assertion, never the service credential, so it cannot read more than that
user can. The server must be started with `--oidc-issuer` (and `--jwks` or discovery). There is no token exchange
step in the current server: the assertion is presented as the bearer token.

### Retries

A request is retried only when repeating it cannot write twice: reads, and writes that carry an idempotency key
(your own, or one derived by `run()`). Retries happen on HTTP 429, 502, 503 and 504 and on network errors, up to
`retries` times (default 3), with full-jitter exponential backoff that honours `Retry-After`. A write without a key
is sent once; if the connection drops mid-write you get a `TransportError` saying it was not retried, and the write
may or may not have been applied. `forget` carries no key in the protocol, so only a dry run is retried.

The adapters and `share()` give each logical write its own random idempotency key, so their writes are retried
safely.

## Adapters

Each adapter declares its conformance level and what its host interface cannot carry (`capabilities()` on the
adapter object). None of them fakes a missing feature.

### LangGraph store (`orp_client.langgraph.OrpStore`, level L2)

```python
from orp_client.langgraph import OrpStore
store = OrpStore(client, budget_tokens=1500)
store.put(("users", "u1", "prefs"), "coffee", {"text": "oat flat white"})
store.search(("users", "u1"), query="what coffee?")
```

* A namespace becomes a scope path under `scope_root` (default `langgraph`), so a search on a namespace prefix is a
  recall within that scope, filtered by the server before ranking. A search without a query lists the namespace.
* A key becomes part of a client id; the value round-trips unchanged through `meta`. By default each principal has
  its own key space (`per_principal_keys=True`), because client ids are unique per tenant and two users writing the
  same namespace and key would otherwise collide.
* `put` is an append, or an upsert guarded by `if_version`; an update keeps the item's readers. `delete` is a
  forget (`delete_mode="retract"` by default) that allows the same value to be put again later.
* The LangGraph interface has no place for `as_of`, budgets or principals. `as_of` and the recall budget come from
  constructor config (or, for `as_of`, from a run-scoped client), and the principal comes from the client's
  credential. New items are readable only by you unless you pass `readers=` or `audience="tenant"`.
* `ttl` is not supported (`supports_ttl = False`), and `put(..., ttl=...)` raises `NotImplementedError`.
* A `filter` combined with a `query` is applied after recall (reported as `langgraph:filter_applied_after_recall`).
  A `filter` without a query is applied to the complete namespace listing, so it misses nothing.
* LangGraph is not imported. If it is installed, `OrpStore` subclasses its `BaseStore` and returns its `Item` and
  `SearchItem`; otherwise the module defines stand-ins with the same fields and operation tuples (`GetOp`,
  `SearchOp`, `PutOp`, `ListNamespacesOp`, `MatchCondition`).

### mem0 (`orp_client.mem0.Memory`, level L2)

```python
from orp_client.mem0 import Memory
m = Memory(client)
m.add("I'm vegetarian", user_id="priya")
m.search("diet", user_id="priya")   # {"results": [{"id", "memory", "hash", "metadata", "score", "created_at", ...}]}
```

* `add`, `search`, `get`, `get_all`, `update`, `delete`, `delete_all` and `history` follow mem0's open-source
  `Memory` signatures and its `{"results": [...]}` return form. `reset` is refused.
* `user_id`, `agent_id` and `run_id` are scope and filters, never the principal. They become scope segments
  `mem0/user/<id>/agent/<id>/run/<id>`. The principal comes from the API key.
* **mem0's own REST API checks the API key but not the user**: any key holder can read any user's memories by
  passing that user's `user_id`. This shim does not repeat that. Records are readable only by the key's principal
  unless you configure other readers, so passing someone else's `user_id` with your key finds nothing of theirs.
* What it loses, reported through `on_degraded`: `infer=True` needs an LLM to extract facts, which the shim does not
  have, so messages are stored as they are (`mem0:infer_not_supported`; `prompt` and `memory_type` are refused).
  `history` lists add, update and delete events with times but not the old text of an update, because ORP's upsert
  replaces content in place and the shim keeps no shadow copies (`mem0:history_without_old_text`). Ids that are not
  a scope prefix (for example `agent_id` without `user_id`) are filtered after recall in `search`
  (`mem0:filter_applied_after_recall`); `get_all` and `delete_all` page through the complete scope, so they miss
  nothing.
* `Memory.from_config({"orp": {"url": ..., "token_env": "ORP_TOKEN"}})` builds the client. A token itself is never
  read from a config dict.

### Letta agent files (`orp_client.letta.import_af`, level L2)

```python
from orp_client.letta import import_af
summary = import_af("my_agent.af", client)
client.profile(2000)   # now includes the agent's core memory blocks
```

* Core memory blocks become `kind: "profile"` records with `load: "always"`, so `profile()` returns them. The label,
  limit and description go in `meta` (`block_label`, `block_limit`, `block_description`). Empty blocks are skipped.
* Messages become `event` records in a thread named after the agent, in order, with their original times.
  JSON-wrapped user messages are unwrapped, and an assistant's `send_message` call becomes its reply. System
  prompts, tool results, heartbeats and login notices are skipped and counted in the summary; inner thoughts are
  left out unless `include_reasoning=True`.
* Archival passages become `document` records.
* Everything is written with `origin: "imported"`, readable only by you unless you pass `readers` or
  `audience="tenant"`, and with idempotency keys derived from the content, so importing the same file twice writes
  nothing new. Tools (which carry source code), LLM and embedding settings and the system prompt are not imported;
  the summary lists them under `not_imported`. The file is parsed as JSON data and nothing in it is evaluated.

**Targeted format.** The single-agent `AgentSchema` that Letta 0.6 and 0.7 export (top-level `core_memory`,
`messages`, `name`, `tools`, `version`; the test fixture is modelled on a 0.6.45 export), and the later multi-entity
layout (top-level `agents` and `blocks`, linked by `block_ids`). Archival passages are read from `passages` or
`archival_memory` lists if present; older exports did not include archival memory at all. The fixture was written
by hand from knowledge of the format, not from a live export, so **check this against a current Letta export before
relying on it**.

## Tests

The tests start a real server (`orp serve --http 0 --memory`) per test class, with its own keys file, and stop it
afterwards. They need Node 22 and the built server in `packages/server`.

```
cd clients/python
python3 -m unittest discover -s tests -t .
```

## Security notes

* Tokens come only from a constructor argument, a callable, or an environment variable you name.
* Logs carry verb names, statuses, attempt numbers and server request ids. Never tokens, never memory content.
* No `pickle`, no `eval`. Bundles, agent files and server responses are parsed as JSON data.
