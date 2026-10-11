# @technodotventures/smartware-mastra

Smartware memory for Mastra agents (what Coffee and the Studio OS run on): memory tools built with your own `createTool`,
and an input/output processor pair that recalls before the model call and records the turn after. Mastra is never
imported: you pass its functions in.

## Host interface targeted

Written from my knowledge of `@mastra/core` 0.x as of mid to late 2025. **Check it against the release you run
before relying on it.** The parts this adapter touches:

- `createTool({ id, description, inputSchema, execute })`, where `inputSchema` is a Zod schema, and `execute` is
  called as `execute({ context, runtimeContext, ... })` (0.x) or `execute(inputData, context)` (later releases).
  The adapter accepts both call shapes.
- `new Agent({ ..., inputProcessors, outputProcessors })`, where a processor is
  `{ name, processInput({ messages, abort }) }` or `{ name, processOutputResult({ messages, abort }) }` and messages
  are `MastraMessageV2` (`{ id, role, content: { format: 2, parts }, threadId, resourceId, createdAt }`).
- Whether a `system` role message is accepted from an input processor. If your release does not accept it, pass
  `toMessage` to build the message your release expects, or use the hook pair with `agent.generate(..., { context })`.

## Setup

```js
import { createTool } from "@mastra/core/tools";
import { Agent } from "@mastra/core/agent";
import { z } from "zod";
import { SmartwareClient } from "@technodotventures/smartware-client";
import { mastraTools, memoryProcessors } from "@technodotventures/smartware-mastra";

const memory = new SmartwareClient({ url: process.env.SMARTWARE_URL, token: process.env.SMARTWARE_TOKEN });
const { input, output } = memoryProcessors(memory, { resourceScope: "users", profileBudget: 300 });

export const agent = new Agent({
  name: "assistant",
  instructions: "Use memory_recall before answering anything that may depend on the past.",
  model,
  tools: mastraTools(memory, createTool, { z }),
  inputProcessors: [input],
  outputProcessors: [output],
});
```

For a platform serving many users, build the client per request from the user's bearer:
`memory.forUser(userAccessToken)`. For a long-running workflow, pass `memory.run({ runId })` so every recall in the
run sees one `as_of` and recorded writes replay safely on retry.

Without processors, the hook pair does the same job by hand:

```js
import { mastraHooks } from "@technodotventures/smartware-mastra";
const hooks = mastraHooks(memory, { thread: threadId });
const context = await hooks.before(messages);
const res = await agent.generate(messages, { context: context ? [{ role: "system", content: context }] : [] });
await hooks.after(messages, { role: "assistant", content: res.text });
```

## Smartware level

L3. Identity is the client's credentials. Mastra's `resourceId` and `runtimeContext` are never used as a principal:
with `resourceScope: "users"`, resourceId `u_1` becomes the scope `users/u_1`, which narrows recall and labels
writes but grants nothing. The tools are `@technodotventures/smartware-tools`, so the model cannot set identity, audience or provenance.

## What it loses, and how it says so

- Processors have no place for `as_of` or budgets per call: they come from options (or a run client).
- The input processor records the user turn when it runs, and the output processor records the reply, as two
  writes. A failure in either throws, so Mastra's own error handling sees it.
- `degraded[]` from the gateway goes to the client's `onDegraded`.

`conformance.json` lists the `smartware doctor` checks this interface can exercise.
