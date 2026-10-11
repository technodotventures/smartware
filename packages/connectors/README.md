# @orp/connectors

Connectors carry source permissions into memory. This package has the connector SDK, two built-in connectors
(a Markdown vault and Slack), and a test kit that proves a connector does not leak. It has no third-party
dependencies and no install scripts. It needs Node 22.13 or later.

```js
import { sync, vaultConnector } from "@orp/connectors";

const vault = vaultConnector({ dir: "/srv/notes", defaultReaders: ["principal:alice"] });
const report = await sync(vault, {
  url: "https://memory.example.com",
  token: process.env.ORP_VAULT_CONNECTOR_KEY, // this connector's own key
  tenant: "acme",
  statePath: "/var/lib/orp/vault-state.json",
});
console.log(report.created, report.updated, report.aclUpdated, report.deleted, report.lag.p95Ms);
```

## Trust model

A connector is a broker, not trusted code.

- **Its own principal, its own process.** Give each connector its own API key (or OAuth client) with no groups. The
  runner asks the server who it is (`/whoami`) and refuses to run as a principal in the `*` or `admin` group.
- **Its own scope prefix.** `defineConnector({ scopePrefix: "vault" })` fixes where it may write. Every record must sit
  at the prefix or below it, by whole path segment (`vault` covers `vault/team`, not `vault-x`). The runner validates a
  whole pull before the first write, so one bad item writes nothing. `ScopedWriter` refuses the same things for code
  that bypasses the runner, refuses records without explicit readers (`null` would mean the whole tenant), and refuses
  to forget a record the connector did not write.
- **Credentials stay in the connector process.** The engine never sees a source credential. The Slack token is read
  from an environment variable whose name the operator passes (`tokenEnv`); it is not an option, is never written to
  the state file, and is sent only to the Slack API over HTTPS (plain HTTP is accepted only for `localhost`, for tests).
- **Nothing sensitive is logged.** The `log` hook receives counts and record ids only, never content or tokens. The
  state file holds hashes, ids, versions and the cursor, no content, and is written with mode 0600.
- **Data is never code.** Frontmatter is read by a small parser that only understands `key: value` and simple lists,
  and `.orp-acl.json`, the membership file and Slack responses are read with `JSON.parse`. Nothing is evaluated.
- **The connector reads only what it wrote.** It is added to the readers of its own records (as
  `principal:<connector id>`), because the server requires read access to forget a record and to report its version.
  It gains no access to anything else.

What the SDK check is not: the scope prefix is enforced in the connector process. The server does not yet bind a
write scope to a key, so a connector key used outside this SDK could still create records elsewhere (though, since
records default to `writers` = their creator, it can never change or forget anyone else's). Binding a scope to a key
at the gateway is listed as open work below.

## How the runner writes

| Source event | ORP write |
|---|---|
| New item | `remember` (append). Client id `<connector>-<sha256(source id)>`: stable, never contains `~` or `/`. |
| Changed content | Read the current version (`GET /memories/{id}`), then `remember` with `mode: "upsert"` and `if_version`. On a version conflict it retries once against the server's current version: the source is the truth for records the connector owns. |
| ACL-only change | The same upsert with the same content and new `readers` only (M57). The engine re-closes every memory derived from it (M59). |
| Move or rename | The record keeps its id; thread, scope and source are updated in place, so derived memories stay attached. |
| Deleted item | `forget` with `cascade: true`, mode `retract` by default (`deleteMode: "purge"` to remove it). If the item comes back, it is restored with `allow_reingest`. |
| Group membership | Stored in the state file and, with `membershipPath`, published for the gateway (see Slack). |

Every write carries `sources: [{uri, hash}]` (the hash is `sha256:` of the content), `scope` under the prefix,
`origin: "observed"`, and an `idempotency_key` of the form `<connector>:<id>:<write number>:<hash>`. The write number
means content that changes A to B and back to A is a real change, while a retry after a lost acknowledgement replays
and changes nothing.

State is a JSON file holding the cursor and a source id to `{id, version, hash, acl, n}` map, written atomically
(temporary file, fsync, rename). The cursor is saved only after a whole pass, so a crash re-pulls, and replays are
idempotent.

Sync lag is measured per item from the source's change time (`changedAt`) to the acknowledged write. `sync` returns
p50, p95 and max for the pass (`report.lag`) and over the last 1000 writes (`report.lagRolling`).

## How ACLs map to readers

`mapAcl` turns each source ACL entry into an ORP access token (spec §6.1):

| Source ACL entry | Reader token |
|---|---|
| `alice` (a source user) | `principal:<principalResolver("alice")>`, or nothing if the resolver returns `null` |
| `group:eng`, `channel:C07`, `role:x`, `team:x`, `org:x`, `principal:x` | kept as written |
| `public` or `*` | `public` (everyone in the tenant, said explicitly) |
| anything else with a colon | dropped |

Unknown users map to nobody. A connector never writes `readers: null`.

## Markdown vault

`vaultConnector({ dir, defaultReaders, split?, scopePrefix?, principalResolver?, ignore? })`

- Every `.md` file is a `document`. By default (`split: "sections"`) each `## ` section is its own record (the text
  before the first `## ` is one more), all in one thread named after the file path, with `seq` in file order. Section
  ids use the heading's slug, so reordering sections does not change ids. `split: "file"` makes one record per file.
- Readers come from the first of: the file's frontmatter `readers:` (`[alice, group:eng]`), the nearest
  `.orp-acl.json` in its folder or a parent (`{"readers": ["alice", "bob"]}`), or `defaultReaders`. A malformed
  `.orp-acl.json` stops the sync rather than falling back to a wider default.
- Each folder is a scope below the prefix (`vault/team`), unless `scopeByFolder: false`.
- Dot folders (`.obsidian`, `.git`, `.trash`) and symlinks are skipped. Files over 1 MiB are skipped.
- A move is detected when a file disappears and a file with the same body appears in the same pass.
- Other frontmatter keys are kept in `meta["x-vault"].frontmatter`.

### Rendering back, and what "two-way" means

`renderVault(url, token, outDir, { tenant })` exports what that token's principal can read and writes the bundle's
Markdown rendering (spec §8.3 `render/`): one file per thread of evidence (documents and events), with frontmatter
holding the record fields (`orp_thread`, `orp_ids`, `orp_versions`, `scope`, `kind`, `readers`, plus the note's
original frontmatter) and a one-line `<!-- orp {...} -->` marker before each record with its id, version, readers,
times and sources. Derived records (summaries, facts, observations) go under `.orp/derived/`, which the vault
connector never ingests. Thread names are used as paths only when they are plain relative `.md` paths; anything else
is slugged, and nothing is written outside `outDir` or through a symlink.

Two-way is at the file level, and honestly limited:

- Files to ORP is the connector. ORP to files is `renderVault`. They are separate one-way passes, not a live merge:
  there is no conflict resolution for a note edited in both places between passes.
- A rendered vault re-ingests to the same records (the test checks this): the markers are stripped on ingest, the
  frontmatter `readers` carries the ACL, and with the same connector name and paths the record ids are identical.
- It is not byte-identical. Whitespace between sections is normalised, frontmatter is rewritten in ORP's own simple
  form, and when the records of one thread have different readers, `readers` is left out (`orp_readers: mixed`) and
  re-ingest falls back to the folder ACL or the default.
- A render shows only what the rendering principal can read, so render with the key of the person who owns the vault.

## Slack

`slackConnector({ tokenEnv, apiBase?, channels?, rescanSeconds?, includeThreads?, principalResolver? })`

- Pulls `conversations.list` (channels the bot is a member of, or the `channels` allowlist), then
  `conversations.history`, `conversations.replies` for threads and `conversations.members`, all with cursor
  pagination. On HTTP 429 it waits for `Retry-After` (capped at 120 s) and retries up to 5 times.
- Each message is an `event`: thread `slack:<channel>` (or `slack:<channel>/<thread_ts>` for replies), actor = the
  Slack user mapped through `principalResolver`, scope `<prefix>/<channel>`, readers `["channel:<channel id>"]`,
  source `slack://<channel>/p<ts>`. Join, leave, topic and pin notices are skipped.
- Edits are detected by `edited.ts`; deletions by a message missing from the re-scanned window (`rescanSeconds`,
  default 7 days).
- Bot token scopes it needs: `channels:read`, `groups:read`, `channels:history`, `groups:history`.

### Membership and the principal resolver

Records say `channel:C07`; who is in C07 is decided at the gateway. The connector publishes membership
(`sync(..., { membershipPath })` writes `{format, connector, tenant, groups: {"channel:C07": ["alice", ...]}}`,
with Slack users already mapped to ORP principal ids), and the gateway must give each user the group
`channel:<id>` for every channel they are in. A principal holding the group `channel:C07` holds the token
`channel:C07` (spec §6.1, M77). When someone leaves a channel, the next sync drops them from the file and they lose
every message and every summary derived from the channel at once, with no record rewritten. Three ways to wire it:

1. **Embedded gateway (what the tests do).** Wrap the authenticator:
   ```js
   import { Authenticator, Gateway, KeyStore } from "@orp/server";
   import { slackPrincipalResolver, withMembership } from "@orp/connectors";
   const auth = withMembership(new Authenticator(new KeyStore("keys.json"), jwt),
     slackPrincipalResolver({ membershipPath: "/var/lib/orp/slack-members.json", tenant: "acme" }));
   ```
   The resolver re-reads the file when it changes. It grants only `channel:` groups (configurable with
   `namespaces`), only from a file written by the named connector, and only in that tenant, so a compromised connector
   cannot grant `admin`, `*` or another namespace.
2. **OIDC claims.** Have your identity provider put the memberships in the access token's groups claim
   (`"groups": ["channel:C07", "channel:C09"]`) and set `groupsClaim` if it is not `groups`. Slack's own OIDC sign-in
   does not carry channel membership, so the provider needs a custom claim fed from the membership file or the Slack
   API. Keep token lifetimes short: a revocation takes effect when the token expires.
3. **API keys.** `orp keys add --id alice --groups channel:C07,channel:C09`. Groups are fixed when the key is issued,
   so a membership change means revoking and re-issuing the key. Suitable only for small, stable teams.

Do not also grant `channel:` groups statically: the resolver adds groups, it never removes them.

`principalResolver` maps a Slack user id to an ORP principal id. You supply it (for example from `users.info` emails
to your IdP's subject ids, looked up in the connector process). Users it returns `null` for stay as a raw `actor`
and are never given channel tokens.

## Test kit

`replayFixture({ connector?, fixture, server, membershipPath? })` replays a scripted source through a connector into
a live server. A fixture is a list of steps: declarative ones for the built-in `fixtureConnector`
(`add`, `edit`, `acl`, `delete`, `members`), or `apply()` functions that change a real source (write vault files,
change a fake Slack API) with `expect` saying who must and must not see which source ids. Any step can run `reflect`
to make derived summaries. After every step it checks, for every fixture principal:

1. **Source truth.** The principal can read exactly what the source ACL allows. An item they lost access to is gone
   from `get` and from `recall`. The truth comes from the fixture (`fixture.resolve`), never from the connector's own
   mapping, so a connector bug cannot hide itself.
2. **Re-narrowing (M59).** No summary made by `reflect` is readable or recalled by a principal who lost access to any
   item it was derived from.
3. **Removal equivalence (M67).** For each query, recall through the server equals recall over a clean in-process
   `Engine` from `@orp/core`, rebuilt from only the records that principal can read (using the server's encoder and
   ranker, in the server's write order, at the same `as_of`), item for item and in order.

It reports per-step failures (leaks are marked `[LEAK]`) and the sync lag. The tests run it against a correct
connector, and against two broken ones (one that never syncs ACL changes, one that maps every ACL to `public`) to
show it catches leaks. The server needs a key in the `*` group (`adminToken`) for the export and `reflect`.

## Conformance claims

With a server that passes L3, a connector built on this SDK and passing `replayFixture` on its own fixture gives:

- **M57**: source ACLs are written into `readers` at ingest and kept in sync by readers-only upserts.
- **M59**: derived memories re-narrow when a source ACL changes (the engine does it; the kit checks it).
- **M67**: removal equivalence per principal, checked against a clean engine.
- **M56**: identity comes from the connector's key, never from a request field.
- **M12**: every record carries its source URI and content hash.

These are claims about the integration under test, not about every source: the vault and Slack connectors are tested
against local fixtures and a fake Slack API, never the real Slack.

## Known limits and open work

- **Server-side scope binding.** The scope prefix is enforced by the SDK, not by the gateway. A key-to-scope binding
  in `orp serve` would make it a hard boundary.
- **Content de-duplication (engine).** The engine de-duplicates a write against an identical record by the same writer
  with the same thread, actor, content and readers, even when an idempotency key is given. Two identical Slack
  messages from one person in one channel (say, two "ok"s) become one record. The runner detects this, reports it as
  `deduplicated`, and never updates or forgets the other item's record through it. An upsert whose new content equals
  another record's is folded the same way inside the engine. Fixing this belongs in the engine.
- **Forget needs read access**, which is why the connector reads its own records (see the trust model).
- **Slack.** Deletions and edits are seen only inside the re-scan window; replies to a thread whose parent is older
  than the window are not picked up; archived channels and channels the bot leaves stop syncing (their members lose
  the channel token). Enterprise Grid, shared channels, files and message attachments are not handled.
- **Membership resolver in `orp serve`.** The CLI has no flag for it yet; today it needs an embedded gateway or the
  OIDC or key routes above.
