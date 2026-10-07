"""Optional LLM-in-the-loop QA evaluation (not run in the sandbox: no model access there).

For each system and question, the packed context (same token budget for every
system) is given to one answering model, and a judge model grades the answer
against the gold answer with the LoCoMo/LongMemEval-style yes/no prompt. Both
prompts and models are pinned below and must be reported with any result.

    ANTHROPIC_API_KEY=... python -m bench.qa --bench locomo --systems orp,mem0,graphiti --budget 1000 --limit 300
"""
from __future__ import annotations

import argparse
import json
import os
import random
import urllib.request

from .data import load_lme, load_locomo
from .encoders import DenseEncoder, Encoders
from .metrics import pack
from .run import make

ANSWER_MODEL = os.environ.get("ORP_QA_MODEL", "claude-haiku-4-5-20251001")
JUDGE_MODEL = os.environ.get("ORP_JUDGE_MODEL", "claude-haiku-4-5-20251001")

ANSWER_PROMPT = """You are answering a question about past conversations using only the memory context below.
Each block starts with the date of the conversation. Resolve relative dates against that date.
If the context does not contain the answer, say "I don't know".

<memory>
{context}
</memory>

Question (asked on {qdate}): {question}
Answer in one short sentence."""

JUDGE_PROMPT = """Gold answer: {gold}
Candidate answer: {pred}
Question: {question}
Does the candidate answer contain the information in the gold answer (dates within the same day/month as stated
in gold, paraphrases allowed)? Reply with exactly one word: CORRECT or WRONG."""


def call(model: str, prompt: str) -> str:
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({"model": model, "max_tokens": 200, "temperature": 0,
                         "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["content"][0]["text"].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="locomo")
    ap.add_argument("--systems", default="orp")
    ap.add_argument("--budget", type=int, default=1000)
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--out", default="results/qa.json")
    a = ap.parse_args()
    enc = Encoders(DenseEncoder.load())
    tasks = load_locomo() if a.bench == "locomo" else load_lme()
    pairs = [(t, q) for t in tasks for q in t.queries if q.split == "test"]
    random.Random(0).shuffle(pairs)
    pairs = pairs[: a.limit]
    res = {}
    for name in a.systems.split(","):
        built = {}
        ok = n = 0
        for task, q in pairs:
            s = built.get(task.corpus.id)
            if s is None:
                s = make(name)
                s.build(task.corpus, enc)
                built[task.corpus.id] = s
            _, _, kept = pack(s.retrieve(q, k=50), a.budget)
            ctx = "\n".join(i.text for i in kept)
            pred = call(ANSWER_MODEL, ANSWER_PROMPT.format(context=ctx, question=q.text, qdate=q.time.date()))
            verdict = call(JUDGE_MODEL, JUDGE_PROMPT.format(gold=q.answer, pred=pred, question=q.text))
            ok += verdict.upper().startswith("CORRECT")
            n += 1
        res[name] = {"accuracy": ok / max(1, n), "n": n, "budget": a.budget, "answer_model": ANSWER_MODEL,
                     "judge_model": JUDGE_MODEL}
        print(name, res[name], flush=True)
    json.dump(res, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
