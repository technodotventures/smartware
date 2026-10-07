# ORP: Open Recall Protocol (working name)

An open protocol for how AI agents and SaaS platforms store, govern and recall memory, with a reference engine, MCP and HTTP servers, a conformance suite and a reproducible benchmark against faithful reimplementations of 15 open-source memory systems.

```
spec/ORP-0.1.md            the protocol (normative)
orp/                       reference engine + service layer + bindings (stdlib + numpy/scipy only)
  engine.py                records, supersession, ACL closure, multi-channel recall, packing
  api.py                   verbs: capabilities, remember, recall, profile, get, neighbours, history,
                           link, forget, feedback, approve, reflect, job, changes, export, import
  http_server.py           HTTP binding (bearer token -> principal)
  mcp_server.py            MCP stdio binding (tools, resources, elicitation for destructive forget)
tests/conformance/         L1-L4 conformance tests (one per MUST)
tests/test_transports.py   end-to-end HTTP and MCP tests
bench/                     ORP-Bench harness: data, shared encoders, metrics, runner, tuning, governance
baselines/                 LLM-free reimplementations of 15 projects' recall paths (+ BM25/dense references)
results/                   summaries, per-query raw results, tuned parameters
```

## Quick start

```bash
python3 -m unittest discover -s tests -t .           # conformance + transports
python3 -m orp.http_server --port 8787 --keys keys.json --store store.json
ORP_PRINCIPAL=me ORP_TENANT=personal python3 -m orp.mcp_server --store ~/.orp/store.json
```

MCP client config:

```json
{"mcpServers": {"orp": {"command": "python3", "args": ["-m", "orp.mcp_server", "--store", "/Users/me/.orp/store.json"],
  "cwd": "/path/to/orp", "env": {"ORP_PRINCIPAL": "me", "ORP_TENANT": "personal"}}}}
```

## Reproducing the benchmark

```bash
python3 -m bench.run --bench locomo,lme --systems all --split test --workers 2
python3 -m bench.governance results/governance.json
python3 -m bench.report                              # writes results/RESULTS.md
```

Benchmarks use the vendored LoCoMo-10 file and the LongMemEval oracle file (with a reconstructed haystack, see `bench/data.py`). All tuning of the reference engine uses dev splits only (`bench/tune.py`, `bench/ltr.py`); test numbers are produced once by `bench/run.py`.
