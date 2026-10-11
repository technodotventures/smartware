# Claude Code and Claude Desktop

Local memory over MCP stdio (the principal is you, from the environment):

```json
{
  "mcpServers": {
    "memory": {
      "command": "orp",
      "args": ["serve", "--store", "/Users/you/.orp/orp.db"],
      "env": { "ORP_PRINCIPAL": "you", "ORP_TENANT": "personal" }
    }
  }
}
```

Hosted memory over Streamable HTTP (the principal comes from the OAuth token, never from tool arguments):

```json
{ "mcpServers": { "memory": { "type": "http", "url": "https://memory.example.com/mcp" } } }
```

Add to CLAUDE.md or the project instructions: "Use the memory tools as described in the `memory_policy` prompt." The default `safe` toolset has recall, remember, forget, profile and history, with no fields that change who can read a memory.

Already using the reference MCP memory server? Run `orp serve --tools safe,mcp-memory` and the nine knowledge-graph tools keep working, now with ranked search and access control.
