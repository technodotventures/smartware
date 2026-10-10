# ORP: Open Recall Protocol (working name), v0.1.3

An open protocol for how AI agents and SaaS platforms store, govern and recall memory, with a reference engine, MCP and HTTP servers, a conformance suite, and a reproducible benchmark against LLM-free re-implementations of the recall paths of 15 open-source memory systems.

```
spec/ORP-0.1.md            the protocol (normative; MUSTs carry stable ids [Mnn])
spec/MUSTS.md              every MUST id, its level and the tests that check it (tools/musts.py)
orp/                       reference engine + service layer + bindings (stdlib + numpy/scipy only)
  engine.py                records, supersession, ACL closure, admissible-set recall, packing
  api.py                   verbs: capabilities, remember, recall, profile, get, neighbours, history,
                           link, forget, feedback, approve, reflect, job, changes, audit_log, export, import
  http_server.py           HTTP binding (bearer token -> principal)
  mcp_server.py            MCP stdio binding (tools, resources, subscriptions, elicitation for forget)
tests/conformance/         conformance tests, named by the MUST ids they check
tests/test_transports.py   end-to-end HTTP and MCP tests
bench/                     ORP-Bench: data, shared encoders, metrics, runner, tuning, governance, QA, report
baselines/                 LLM-free re-implementations of 15 projects' recall paths (+ BM25/dense references)
reports/ (outside repo)    code-level studies of the 15 projects that the spec and baselines cite
results/                   RESULTS.md, METHODOLOGY.md, two audits and responses, raw per-query results
```

## Quick start

```bash
python3 -m unittest discover -s tests -t .           # conformance + transports
python3 -m orp.http_server --port 8787 --keys keys.json --store state.bin
ORP_PRINCIPAL=me ORP_TENANT=personal python3 -m orp.mcp_server --store ~/.orp/state.bin
```

MCP client config:

```json
{"mcpServers": {"orp": {"command": "python3", "args": ["-m", "orp.mcp_server", "--store", "/Users/me/.orp/state.bin"],
  "cwd": "/path/to/orp", "env": {"ORP_PRINCIPAL": "me", "ORP_TENANT": "personal"}}}}
```

The reference servers keep their state in a server-owned pickle file; never point them at a file from an untrusted source. Bundles for moving memory between engines use `export` / `import` (§8.3).

## Reproducing the benchmark

```bash
python3 -m bench.train_orp && python3 -m bench.train_ablation --ship      # ORP, dev splits only
python3 -m bench.tune_baselines all                                         # baselines, dev splits only
python3 -m bench.run --bench locomo,lme --systems orp,orp-rrf,orp-allfeat,bm25,dense --out results/summary_v012.json --raw results/raw_v012
python3 -m bench.run --bench locomo,lme --systems graphiti@tuned,...,memoryos@tuned --out results/summary_tuned.json --raw results/raw_v012
python3 -m bench.run --bench hotpot --systems all --out results/summary_hotpot.json --raw results/raw_v012
python3 -m bench.run --bench lmem --systems orp,orp-rrf,graphiti@tuned,...,bm25,dense --out results/summary_lmem.json --raw results/raw_lmem --resume
python3 -m bench.governance results/governance_v50.json 0.5 && python3 -m bench.governance results/governance_v15.json 0.15
python3 -m bench.latency orp,orp-rrf,graphiti,...                           # on an idle machine
python3 -m bench.report                                                     # writes results/RESULTS.md
```

Data: the vendored LoCoMo-10 file, the LongMemEval oracle file with recombined haystacks (LME-R, see `bench/data.py`), and Cognee's HotpotQA-50 set (held-out, added after the v0.1.1 freeze). Every test-split invocation is logged in `results/TEST_RUNS.log`. Methodology and limitations: `results/METHODOLOGY.md`.

Datasets are read from upstream repositories cloned next to this one (`../vendor/`): `snap-research/locomo` (LoCoMo-10), `getzep/graphiti` (`tests/evals/data/longmemeval_data/longmemeval_oracle.json`) and `topoteretes/cognee` (`evals/old/hotpot_50_*.json`). The official LongMemEval-M file (`longmemeval_m_cleaned.json`, xiaowu0162/longmemeval) is read from `../data/lme_m/` after deduplicating its sessions with `tools/compact_lme_m.py`. They are not redistributed here. The code studies of the 15 projects are in `../reports/`.
