# smartware

`smartware`: one binary for Smartware memory.

- `smartware serve`: MCP over stdio (principal from the environment), or `--http <port>` for the HTTP binding at
  `/v1/<tenant>/...`, MCP Streamable HTTP at `/mcp` (OAuth 2.1 resource server, principal-bound sessions, resource
  subscriptions), and a mem0-compatible REST API at `/mem0`.
- `smartware doctor`: conformance by MUST id against any endpoint, and removal-equivalence checks for your real principals.
- `smartware eval`: recall and QA on your data with your reader.
- `smartware keys`, `smartware backup`.

Install pinned, without scripts: `npm install -g smartware@2.0.0 --ignore-scripts`, then check the release checksum.
See docs/OPERATIONS.md for deployment.
