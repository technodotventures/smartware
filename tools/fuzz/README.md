# Fuzz harnesses (from the third audit)

- `persist_fuzz.mjs [seeds] [steps]`: random verbs against a store-backed service. After every step a fresh service
  opened on the same SQLite file must be indistinguishable from the live one (exports, recall packs, change feeds,
  audit, idempotency and re-ingest state), and a failing write must leave every table unchanged.
- `diff_fuzz.py [seeds] [steps] [seed0] [--frozen-clock] [--idprob=1] [--reopen]`: the same random operation
  sequences through the Python oracle and the TypeScript service (via `diff_server.mjs`), comparing every response.
  Use `--idprob=1` (client-chosen ids) for a strict comparison: engine-assigned ids are random on each side, so with
  them near-ties can break differently, which shows up as noise rather than a defect. `--reopen` saves and reopens
  the TypeScript service from SQLite every 25 steps.

Both exit non-zero on any failure. `npm run fuzz` runs the long versions; CI runs short ones.
