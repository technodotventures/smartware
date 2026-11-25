# Smartware on DolphinBench

[DolphinBench](https://dolphinbench.ai) grades what an agent *does* with real tools after months of conversation
history: a test passes only when the right tool is called, on the right target, with the right content. This
directory connects Smartware to its participant runner (`docs/DRIVER_CONTRACT.md` in
[mem0ai/dolphinbench](https://github.com/mem0ai/dolphinbench)).

- `smartware_harness.py`: the harness (`BenchmarkHarness`). A plain tool-calling agent on any OpenAI-compatible
  endpoint (OpenRouter by default), with Smartware as its memory, running as a local server on loopback.
- `trial.py`: one persona, optionally the first N tests, through the official runner, apps and grader. Not a
  submission.
- `run.yaml.example`: a configuration to copy.

## What the harness does

**Ingestion** (memory writable, one fresh conversation per history message):
1. Stores the message as raw evidence: an `event` with its narrative date.
2. Recalls memory as of that date, then lets the agent answer with the app tools and `memory_recall`.
3. Stores the agent's reply as an event.
4. Runs Smartware extraction on the message (`reflect` with kinds fact, preference and procedure). It writes rules
   and preferences as active standing context derived from the user's own message, and replaces the memories the
   message changes.

**Tests** (memory read-only):
- The memory pack for the request, as of its narrative date, goes into the system prompt: standing rules and
  preferences first, then what matches.
- The agent has the app tools plus a read-only `memory_recall`.
- The Smartware credential is a read-only key (`smartware keys add --read-only`), so no write can reach the store.
- `verify_checkpoint` compares the hash of every stored record with the one taken at `freeze`.

**Cost:**
- The ledger (`<output>/smartware/ledger.jsonl`) records every agent call and every extraction job, with the
  provider-reported cost. OpenRouter reports it.
- For providers that do not report cost, set `options.prices`.
- `total_cost_usd` refuses to answer if any call has no cost, rather than counting it as zero.

**Integrity:**
- Nothing here reads dataset files, facts, grading criteria or test specs; the runner passes one message at a time.
- `trial.py` only takes the first N entries of the test list.
- The prompts are general (`SYSTEM` in the harness, `EXTRACT_PROMPT` in `packages/core/src/extract.ts`), and their
  hashes are in `identity()`.

## Running it

Run everything from a DolphinBench checkout at a reviewed commit (Python 3.12+ and Node 22+):

```bash
pip install -r requirements.txt
cp <smartware>/tools/dolphinbench/run.yaml.example run.yaml        # then edit the model names
export PYTHONPATH=<smartware>/tools/dolphinbench
export OPENROUTER_API_KEY=...                                       # agent and extraction
export AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_API_KEY=...           # the fixed grader (gpt-5.6-sol, medium)
python -m harness.runner prepare                                    # local checks, no model calls
```

A trial on one persona, which reports accuracy and cost:

```bash
python <smartware>/tools/dolphinbench/trial.py --config run.yaml --persona morgan --tests 50 --confirm-paid-calls
```

A full run and submission, all three personas and 600 tests:

```bash
python -m harness.runner ingest --confirm-paid-calls
python -m harness.runner evaluate --confirm-paid-calls
python -m harness.runner package
```

Use a new `output` directory whenever the model, the options or the code change.

## Rough token volumes per persona

These are estimates, not measurements; the trial measures them.
- The app tool definitions are about 9,000 tokens (Morgan, 31 tools).
- The memory pack and prompt add about 2,500 tokens.

| Stage | With `ingest_app_tools: true` | With `false` |
|---|---|---|
| Ingestion: agent (about 3,400 to 5,100 messages) | about 40M to 90M input tokens | about 9M to 14M |
| Ingestion: extraction (one call per message) | about 5M to 8M | same |
| Tests (200 requests, a few calls each) | about 7M | same |

To estimate cost, multiply these volumes by your model's price per million tokens. Output tokens are a small
fraction of the total. Grading is billed separately to the Azure account.

## Checked without paid calls

The harness has been run end to end against a fake OpenAI-compatible model, using the real DolphinBench app
servers and runner code. That run covered:
- ingestion, the checkpoint and the cost ledger;
- a test interaction using `memory_recall` and an app tool;
- the read-only key refusing a write;
- the checkpoint unchanged after the test.
