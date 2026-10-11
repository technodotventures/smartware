# Mastra (Coffee, Studio OS)

```ts
import { createTool } from "@mastra/core/tools";
import { SmartwareClient } from "@technodotventures/smartware-client";
import { mastraTools, memoryProcessors } from "@technodotventures/smartware-mastra";

const memory = new SmartwareClient({ url: process.env.SMARTWARE_URL!, getToken: () => process.env.SMARTWARE_TOKEN! });
const { input, output } = memoryProcessors(memory, { budgetTokens: 1500 });
const agent = new Agent({
  name: "coffee",
  instructions: [MEMORY_POLICY, "...your instructions..."].join("\n\n"),
  tools: { ...mastraTools(memory, createTool) },
  inputProcessors: [input],   // recall before the model runs
  outputProcessors: [output], // remember the turn afterwards
});
```

`MEMORY_POLICY` is `policy/memory-policy.md`. Check the processor option names against your Mastra release (see packages/adapters/mastra/README.md). For a platform serving many users, build one client per user with `memory.forUser(userAccessToken)` so each user's memories stay theirs.
