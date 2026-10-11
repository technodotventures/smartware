# Security

## Reporting
Report vulnerabilities privately to security@techno.ventures (placeholder address, confirm before publishing). Please do not open a public issue. We aim to acknowledge within two working days.

## What the kit guarantees, and how it is tested
- **Identity comes from credentials only.** No verb, tool or adapter accepts a principal, tenant or reader set from a request body as identity. Tested by `orp doctor` (M56) on every build.
- **Admissibility before ranking.** A record you cannot read never changes what you recall, including scores and order (M67). Tested by the doctor against both engines, and checked on your own data with `orp doctor --principals`.
- **Fail-closed writes.** The MCP `safe` toolset and the clients default every write to the writer's own audience; widening is an explicit share.
- **Data is never code.** Stores are SQLite with JSON text; encoder files are data-only `.orpenc` with a checksum; bundles and connector payloads are parsed as JSON. Pickle files are refused unless explicitly allowed for a file you created yourself.
- **Secrets in content.** Text that looks like a credential is labelled `secret` on write and is not recalled without an explicit request and the `label:secret` token. This is a safety net, not a guarantee: do not paste credentials into chats.

## Supply chain
- No npm lifecycle scripts in any package, no third-party runtime dependencies, SQLite from the Node runtime (nothing compiles on install). `tools/release-check.mjs` enforces this, scans for the 2026 payload signatures and over-long lines, and writes an SBOM and checksums. CI runs it on every push; `tools/hooks/pre-push` runs it before every push.
- Releases publish only from tags through npm trusted publishing with provenance, behind a reviewed `release` environment. There are no long-lived npm tokens.
- Install a pinned version and check its published checksum: `npm install @orp/server@0.2.0 --ignore-scripts`, then compare against `SHA256SUMS` from the release.

## Repository settings checklist (one-off, by an owner)
- Secret scanning and push protection on at creation.
- Branch protection on `main` with `enforce_admins: true`; required check: `ci`.
- npm org: 2FA required for all members; no granular tokens that bypass 2FA; second maintainers added by decision, not team default.
