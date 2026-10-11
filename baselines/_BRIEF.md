# Brief: implement your project's LLM-free retrieval baseline

Harness lives in /home/claude/mp/smartware (Python 3.13, numpy/scipy/sklearn/networkx available, no network, no pip installs).
Read these first: bench/system.py (System/Item interface), bench/encoders.py (shared BM25 + dense embedder),
bench/text.py (shared tokeniser/stemmer/token counter), bench/data.py (Turn/Corpus/Query), baselines/reference.py (tiny example).

Write ONE file, baselines/<module>.py, defining the class named in your message. Implement section 13 of your own report
(/home/claude/mp/reports/<slug>.md) as faithfully as possible. Rules:
1. Faithful, not improved. Use the project's real constants (k, RRF constants, weights, thresholds, decay, chunk sizes,
   expansion rules). Where the project ships several modes, use the strongest one it ships by default that does not need
   an LLM or paid reranker. If your recipe has a "fixed/upgraded" variant, it may exist as an option but must NOT be the default.
2. Encoders: use only enc.bm25(docs) and enc.embed / enc.embed_many (unit-norm vectors, cosine = dot). Do not build your own
   neural encoders. Do not use project code. Postgres ts_rank-style keyword scoring may be approximated with the shared BM25,
   or reimplemented if the difference matters to fidelity (say which).
3. Embedder-scale thresholds (e.g. "cosine >= 0.6"): our embedder's cosine scale differs from the project's. Convert each such
   threshold to the equivalent percentile using the documented model's typical distribution if you know it, otherwise keep it
   but check (on a DEV corpus only) that it does not filter out nearly everything or nothing; document the decision.
4. No LLM: approximate extraction as your recipe says (turn/sentence units, regex entities, pronoun rewrite, etc.).
5. Item.text must be exactly what the project would put into the agent's context (date prefixes, fact text, session
   chunk...). Token budgets are charged on it. Item.turn_ids = the source turns that text actually contains.
   Return up to k items (ranked); if the project returns fewer by design, return fewer.
6. You may use q.time (the question's timestamp) only where the project uses a "now"; note that several projects use
   wall-clock now: in that case use q.time (the benchmark's "now"), and say so.
7. Do NOT run bench.run and do NOT compute recall/accuracy on any split. The only allowed check is:
     cd /home/claude/mp/smartware && python3 -m bench.smoke <system-name>
   which prints no scores. It must pass and stay under ~2 s per query.
8. Put a module docstring listing: what is faithful, what is approximated, parameters, and any deliberate deviations.
   Also set class attributes name="<system-name>", family="<one of: memory-sdk | company-brain | personal-brain |
   memory-os | reference-server | graph-engine>", faithful_notes="<one paragraph>".
9. Do not edit any other file in the repo.
Reply with: file path, smoke output, and a 5-line fidelity summary.
