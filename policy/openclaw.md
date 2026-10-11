# OpenClaw

OpenClaw agents run in swarms, so identity matters more than usual: every agent gets its own key, and the server decides who each agent is (server-anchored sessions), never the agent's own `actor_id`.

```
orp keys add --keys keys.json --id openclaw-planner --tenant studio --groups swarm --write-scope agents/openclaw/planner
orp keys add --keys keys.json --id openclaw-coder   --tenant studio --groups swarm --write-scope agents/openclaw/coder
```

Add to each agent's instructions: "Follow the `memory_policy` prompt. Share a memory with the swarm only when another agent needs it: say why when you do." Memories are private to the writing agent by default; sharing with the swarm means readers `group:swarm`.
