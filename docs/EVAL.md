# orp eval

Re-measure recall on your own data, with the reader you will actually use. Nothing leaves your machine except calls
to the reader endpoint you configure; keys come only from environment variables and are never logged or written.

## Data
One haystack per line (`orp-bench-1`):

```json
{"id": "h1", "memories": [{"id": "u1", "content": "I adopted a beagle called Biscuit", "actor": "user", "role": "user",
  "thread": "s1", "seq": 0, "observed_at": "2025-04-02T10:00:00"}],
 "questions": [{"id": "q1", "question": "What breed is my dog?", "as_of": "2025-07-01T00:00:00", "answer": "beagle",
  "evidence": ["u1"], "category": "single-hop"}]}
```

`evidence` lists the memory ids that hold the answer (for evidence recall); `answer` is used for answer-term recall
and, with a reader, for grading. The published sets: `python3 tools/eval/export_bench.py locomo test locomo.jsonl`
(and `lmes`, `lmem` with the LongMemEval files).

## Run

```bash
orp eval --data locomo.jsonl --encoder sif:sif-ppmi-256.orpenc --systems orp,bm25,dense --out results/
ORP_EVAL_KEY=... orp eval --data mine.jsonl --reader openai:gpt-4o-mini --judge anthropic:claude-haiku-... --judge-key-env ANTHROPIC_API_KEY
orp eval --data mine.jsonl --reader openai:llama3.1@http://127.0.0.1:11434/v1      # a local model
```

## What you get
- R@B: share of gold evidence inside a B-token context (B = 500, 1000, 2000, 4000; tokens = characters/4, greedy
  packing). E@B counts a unit only if the packed text carries at least half its content terms. Ans@B: share of the
  answer's terms in the context. SessAll@5, MRR.
- With a reader: QA accuracy per system, plus the **ceiling**: the same reader given only the gold evidence. A low
  ceiling means the reader, not the memory, is the bottleneck. The blind QA in results/ showed that the lead between
  systems can change with the reader, so test yours.
- Every number has a 95% bootstrap interval; differences against the first system are paired. There is no single
  headline score.
- `summary.json` and `per_question.jsonl` in `--out`.
