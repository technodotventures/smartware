# @technodotventures/smartware-tools

Memory tools and hooks for any agent framework, with no framework imported. The other adapters build on it.

## Host interface

Tool objects of the shape most frameworks accept, one way or another:

```ts
{ name, description, inputSchema /* JSON Schema */, annotations, execute(args) }
```

and a hook pair for recall-before and remember-after around a model call:

```ts
{ before(messages) -> context text, after(messages, reply) }
```

Messages can be strings, `{ role, content }` with string or `[{ type: "text", text }]` content, Mastra's
`{ content: { parts } }` or AI SDK UI `{ parts }`.

## Setup

```js
import { SmartwareClient } from "@technodotventures/smartware-client";
import { createMemoryTools, memoryHooks } from "@technodotventures/smartware-tools";

const memory = new SmartwareClient({ url, token });
const tools = createMemoryTools(memory, { scope: `users/${hostUserId}` });
// tools.recall, tools.remember, tools.forget, tools.profile
const result = await tools.recall.execute({ query: "what does the user drink?" });

const hooks = memoryHooks(memory, { thread: conversationId });
const context = await hooks.before(messages);          // add to the system prompt
const reply = await callModel([{ role: "system", content: context }, ...messages]);
await hooks.after(messages, reply);                     // records the user turn and the reply as events
```

Pass `memory.run({ runId })` instead of `memory` to pin `as_of` for the run and make recorded writes replay-safe.

## What the model can and cannot do

The tools mirror the gateway's `safe` MCP toolset:

| Tool | Arguments |
|---|---|
| `memory_recall` | `query`, `as_of`, `budget_tokens` (50 to 8000), `include_history`, `kinds`, `min_coverage` |
| `memory_remember` | `content`, `kind` (fact, preference or procedure), `claim`, `event_time`, `idempotency_key` |
| `memory_forget` | `ids`, `dry_run`, `reason` (always a retract) |
| `memory_profile` | `subject`, `budget_tokens` |

There are no identity, audience or provenance fields (principal, tenant, readers, writers, actor, origin, scope,
labels, sources). Any unknown argument is rejected with `unsupported_parameter`, not ignored, so a prompt injection
cannot ask for someone else's memory or widen who sees a write. Two deliberate differences from the MCP safe set:

- `kind: "event"` is not offered. What a model says is not observed evidence. The hooks record real conversation
  turns as events instead.
- `actor` and `thread` are not offered. The thread is configuration (`thread` option).

What the developer sets in options: `scope` (for example a host user id as a scope segment, which narrows recall
and labels writes but never grants access), `thread`, `audience` (`"self"` by default, or `"tenant"`), budgets and
`confirmForget` (called with the affected ids before a real forget; return false to cancel).

Procedures are stored as drafts that a person approves. Writes carry `meta.via: "tool"`, and the gateway records the
authenticated writer as `written_by`.

## Smartware level

L3 for what the tools expose: identity comes from the client's credentials and the gateway enforces readers. The
tools cannot set readers, ids or derivations, so `conformance.json` lists which `smartware doctor` checks this interface
can exercise and why the rest cannot be.

## What it loses, and how it says so

- No `known_at`, no explicit readers, no ids: by design.
- `degraded[]` from the gateway is returned in each tool result and also goes to the client's `onDegraded`.
- Repeated identical turns in the same thread are de-duplicated by the engine (content dedup is per writer, thread,
  actor and content).

## Exports

`createMemoryTools`, `memoryHooks`, `formatContext`, `messageText`, `lastUserText`, `validate` (the small JSON
Schema checker the tools use), `toZod` and `toZodShape` (for hosts that only accept Zod; pass your own `z`).
