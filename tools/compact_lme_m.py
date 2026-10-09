"""Compact the official longmemeval_m_cleaned.json (2.7 GB) into deduplicated files bench/data.py:load_lme_m reads.

    python3 tools/compact_lme_m.py longmemeval_m_cleaned.json ../data/lme_m

Streams the top-level array (works in 3 GB of RAM). Writes questions.jsonl.gz (every field except the sessions, plus
each session's content hash) and sessions_NN.jsonl.gz (one record per unique session id). Reports any session id that
appears with two different contents (there are none in the cleaned file).
"""
import gzip, hashlib, json, os, sys

src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
dec = json.JSONDecoder()
f = open(src, "r", encoding="utf-8")
buf, pos, eof = "", 0, False


def more():
    global buf, pos, eof
    c = f.read(16 << 20)
    if not c:
        eof = True
    buf, pos = buf[pos:] + c, 0


qf = gzip.open(os.path.join(out, "questions.jsonl.gz"), "wt", compresslevel=6)
part, sf, written = 0, None, 0


def newpart():
    global sf, part, written
    if sf:
        sf.close()
    sf = gzip.open(os.path.join(out, f"sessions_{part:02d}.jsonl.gz"), "wt", compresslevel=9)
    part, written = part + 1, 0


newpart()
seen, conflicts, nq, refs = {}, 0, 0, 0
more()
while True:
    while True:
        while pos < len(buf) and buf[pos] in " \n\r\t,[":
            pos += 1
        if pos < len(buf) or eof:
            break
        more()
    if pos >= len(buf) or buf[pos] == "]":
        break
    while True:
        try:
            obj, end = dec.raw_decode(buf, pos)
            break
        except json.JSONDecodeError:
            if eof:
                raise
            more()
    pos = end
    sids, sess = obj.pop("haystack_session_ids"), obj.pop("haystack_sessions")
    for sid, s in zip(sids, sess):
        refs += 1
        h = hashlib.sha1(json.dumps(s, sort_keys=True).encode()).hexdigest()
        if sid in seen:
            conflicts += seen[sid] != h
            continue
        seen[sid] = h
        line = json.dumps({"sid": sid, "h": h, "turns": s}) + "\n"
        sf.write(line)
        written += len(line)
        if written > 900 << 20:
            newpart()
    obj["haystack_session_ids"], obj["haystack_hashes"] = sids, [seen[s] for s in sids]
    qf.write(json.dumps(obj) + "\n")
    nq += 1
qf.close()
sf.close()
print("questions", nq, "unique sessions", len(seen), "session references", refs, "id conflicts", conflicts)
