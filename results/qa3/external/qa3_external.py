#!/usr/bin/env python3
"""Blind QA v3, external (non-Claude) reader and judge, run on your own machine through OpenRouter.

Usage (in Terminal, from this folder):
    read -s OPENROUTER_API_KEY && export OPENROUTER_API_KEY
    python3 qa3_external.py                  # default model: deepseek/deepseek-chat
    python3 qa3_external.py --model openai/gpt-4.1

Reads items.json (uid, question, asked_on, context; systems hidden) and judge_meta.json (uid -> question, type,
gold; systems hidden). Writes answers.jsonl and verdicts.jsonl next to this script, resuming if interrupted.
The key is read only from the environment and is never written anywhere. Standard library only.
"""
import argparse, json, os, sys, threading, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "https://openrouter.ai/api/v1/chat/completions"
READER = open(os.path.join(HERE, "READER_V3.md")).read()
JUDGE = open(os.path.join(HERE, "JUDGE_V3.md")).read()
lock = threading.Lock()


def call(model, system, user, key, max_tokens):
    body = json.dumps({"model": model, "temperature": 0, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
    for attempt in range(6):
        try:
            req = urllib.request.Request(URL, body, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)["choices"][0]["message"]["content"].strip()
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, TimeoutError) as e:
            code = getattr(e, "code", None)
            if code in (401, 402, 403):
                sys.exit(f"OpenRouter refused the request ({code}): check the key and credit.")
            time.sleep(2 ** attempt)
    return None


def done(path):
    out = {}
    if os.path.exists(path):
        for line in open(path):
            try:
                d = json.loads(line)
                out[d["uid"]] = d
            except Exception:
                pass
    return out


def run(stage, items, path, fn, workers):
    have = done(path)
    todo = [it for it in items if it["uid"] not in have]
    print(f"{stage}: {len(have)} done, {len(todo)} to go", flush=True)
    n = [0]
    with open(path, "a") as f:
        def work(it):
            res = fn(it)
            if res is None:
                return
            with lock:
                f.write(json.dumps({"uid": it["uid"], stage: res}) + "\n")
                f.flush()
                n[0] += 1
                if n[0] % 100 == 0:
                    print(f"  {stage} {n[0]}/{len(todo)}", flush=True)
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(work, todo))
    return done(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek/deepseek-chat")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("Set OPENROUTER_API_KEY first (read -s OPENROUTER_API_KEY && export OPENROUTER_API_KEY).")
    items = json.load(open(os.path.join(HERE, "items.json")))
    meta = json.load(open(os.path.join(HERE, "judge_meta.json")))

    def read(it):
        u = (f"Answer this one item. Reply with the answer only, no JSON, no explanation.\n\n"
             f"Question: {it['question']}\nAsked on: {it['asked_on']}\n\nMEMORY CONTEXT:\n{it['context'] or '(empty)'}")
        return call(a.model, READER, u, key, 200)

    ans = run("answer", items, os.path.join(HERE, "answers.jsonl"), read, a.workers)

    def judge(it):
        m = meta[it["uid"]]
        u = (f"Grade this one item. Reply with exactly one word: CORRECT or WRONG.\n\nQuestion: {m['question']}\n"
             f"Question type: {m['question_type']}\nGold answer: {m['gold']}\nCandidate answer: {ans[it['uid']]['answer']}")
        r = call(a.model, JUDGE, u, key, 5)
        return None if r is None else ("CORRECT" if r.upper().startswith("CORRECT") else "WRONG")

    run("verdict", [it for it in items if it["uid"] in ans], os.path.join(HERE, "verdicts.jsonl"), judge, a.workers)
    json.dump({"model": a.model}, open(os.path.join(HERE, "run_info.json"), "w"))
    print("finished. Tell Claude the files are ready.")


if __name__ == "__main__":
    main()
