# @technodotventures/smartware-ai-sdk

Smartware memory for the Vercel AI SDK as language-model middleware, plus helpers for the OpenAI Agents SDK and the Claude
Agent SDK. None of those packages is imported: you pass their functions in.

## Host interfaces targeted

Written from my knowledge of these releases as of 2025. **Check each against the release you run.**

- **AI SDK 5** (`ai` 5.x): `wrapLanguageModel({ model, middleware })` with a `LanguageModelV2Middleware`:
  `{ middlewareVersion: "v2", transformParams({ type, params }), wrapGenerate({ doGenerate, params }),
  wrapStream({ doStream, params }) }`. `params.prompt` is an array of `{ role, content }` where user content is
  `[{ type: "text", text }]`; per-call settings are `params.providerOptions`; `doGenerate()` returns `{ content }`
  and stream text parts are `{ type: "text-delta", delta }`.
- **AI SDK 4** (`ai` 4.x), with `version: "v1"`: `LanguageModelV1Middleware`, `params.providerMetadata`,
  `doGenerate()` returning `{ text }` and stream parts `{ type: "text-delta", textDelta }`.
- **OpenAI Agents SDK** (`@openai/agents` 0.1.x): `tool({ name, description, parameters, strict, execute })`,
  where `parameters` may be JSON Schema when `strict: false`.
- **Claude Agent SDK** (`@anthropic-ai/claude-agent-sdk` 0.1.x): `tool(name, description, zodRawShape, handler)`,
  `createSdkMcpServer({ name, version, tools })`, `options.mcpServers`, and `options.hooks.UserPromptSubmit`
  returning `{ hookSpecificOutput: { hookEventName: "UserPromptSubmit", additionalContext } }`.

## Vercel AI SDK

```js
import { wrapLanguageModel, generateText } from "ai";
import { SmartwareClient } from "@technodotventures/smartware-client";
import { memoryMiddleware } from "@technodotventures/smartware-ai-sdk";

const memory = new SmartwareClient({ url: process.env.SMARTWARE_URL, getToken: () => session.accessToken });
const model = wrapLanguageModel({ model: baseModel, middleware: memoryMiddleware(memory, { budgetTokens: 1500 }) });

const { text } = await generateText({
  model,
  messages,
  providerOptions: { memory: { thread: conversationId /*, scope, asOf */ } },
});
```

- `transformParams` takes the last user message as the query, recalls, and inserts the context block as a system
  message after any leading system messages.
- `wrapGenerate` and `wrapStream` record the user turn as an event in the thread (from `providerOptions.memory.thread`,
  else the `thread` option). With `recordReply: true` they also record the assistant's reply; the stream is passed
  through chunk for chunk and the turn is recorded when it ends.
- `providerOptions.memory` accepts only `thread`, `scope` and `asOf`. Anything else (principal, userId, tenant,
  readers) is rejected with `unsupported_parameter`: identity comes from the client's credentials and the audience
  from the `audience` option (`"self"` by default).
- If recall fails, the call carries on without memory and `ai-sdk:recall-failed` goes to the client's `onDegraded`
  (set `failOpen: false` to fail the call instead). If recording fails, the reply is still returned and the error
  goes to `onError` (default: a warning with the error code, never content).

For a platform serving many users, build the client per request: `memory.forUser(userAccessToken)`. For an agent
loop, pass `memory.run({ runId })` so every step sees one `as_of` and recorded turns replay safely.

## OpenAI Agents SDK

```js
import { Agent, run, tool } from "@openai/agents";
import { openAIAgentsTools, recallContext, recordTurn } from "@technodotventures/smartware-ai-sdk";

const agent = new Agent({ name: "assistant", instructions: "Use memory_recall before answering about the past.",
  tools: openAIAgentsTools(memory, tool) });

// or recall up front and record afterwards
const context = await recallContext(memory, input);
const result = await run(agent, context ? `${context}\n\n${input}` : input);
await recordTurn(memory, { user: input, reply: result.finalOutput }, { thread: conversationId });
```

## Claude Agent SDK

The native route is MCP: point the SDK at the gateway's MCP endpoint, which serves the same safe toolset at L4.

```js
import { query } from "@anthropic-ai/claude-agent-sdk";
const options = { mcpServers: { memory: { type: "http", url: `${SMARTWARE_URL}/mcp`, headers: { Authorization: `Bearer ${token}` } } } };
```

In-process tools and a prompt hook, when you would rather not run MCP over HTTP:

```js
import { query, tool, createSdkMcpServer } from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";
import { claudeAgentTools, claudeAgentHooks, recordTurn } from "@technodotventures/smartware-ai-sdk";

const server = createSdkMcpServer({ name: "memory", version: "0.2.0", tools: claudeAgentTools(memory, tool, z) });
for await (const msg of query({ prompt, options: {
  mcpServers: { memory: server },
  hooks: claudeAgentHooks(memory, { thread: sessionId }),   // recall into additionalContext, record the prompt
} })) { /* ... */ }
```

The SDK's hooks do not carry the assistant's reply, so record it with `recordTurn` after the loop if you want it.
Tool errors come back as results with `isError: true`, so the model can read them.

## Smartware level and what it loses

**L3.** Identity is the client's credentials; per-call `asOf`, `thread` and `scope` fit in `providerOptions`.
What the middleware cannot carry per call: recall budgets and audience (both are options). `degraded[]` from the
gateway goes to the client's `onDegraded`. The tool helpers are `@technodotventures/smartware-tools` and have the same limits.

`conformance.json` lists the `smartware doctor` checks these interfaces can exercise.
