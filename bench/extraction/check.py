"""Validate extraction outputs: every input session covered, every cited turn id real."""
import glob, json, os, sys
bad = []
for f in sorted(glob.glob("results/extraction/in/" + (sys.argv[1] if len(sys.argv) > 1 else "*") + ".json")):
    o = f.replace("/in/", "/out/").replace(".json", ".jsonl")
    inp = json.load(open(f))
    ids = {t["id"] for s in inp for t in s["turns"]}
    if not os.path.exists(o):
        bad.append((os.path.basename(f), "missing")); continue
    rows, broken = [], 0
    for l in open(o):
        if l.strip():
            try: rows.append(json.loads(l))
            except Exception: broken += 1
    miss = {s["session"] for s in inp} - {r.get("session") for r in rows}
    badc = sum(1 for r in rows if not r.get("turn_ids") or not set(r["turn_ids"]) <= ids)
    if miss or badc > len(rows) * 0.05 or broken:
        bad.append((os.path.basename(f), len(rows), f"miss={len(miss)}", f"badcite={badc}", f"broken={broken}"))
print("problems:", bad)
