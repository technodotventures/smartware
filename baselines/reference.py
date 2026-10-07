"""Plain reference retrievers (not any project): turn-level BM25 and turn-level dense."""
from __future__ import annotations

import numpy as np

from bench.system import Item, System, fmt_turn


class BM25Only(System):
    name = "bm25"

    def build(self, corpus, enc):
        self.turns = corpus.turns
        self.texts = [fmt_turn(t) for t in self.turns]
        self.idx = enc.bm25(self.texts)

    def retrieve(self, q, k=30):
        s = self.idx.score(q.text)
        top = np.argsort(-s)[:k]
        return [Item([self.turns[i].id], self.texts[i], float(s[i])) for i in top]


class DenseOnly(System):
    name = "dense"

    def build(self, corpus, enc):
        self.enc = enc
        self.turns = corpus.turns
        self.texts = [fmt_turn(t) for t in self.turns]
        self.E = enc.embed_many([f"{t.speaker}: {t.text}" for t in self.turns])

    def retrieve(self, q, k=30):
        s = self.E @ self.enc.embed(q.text)
        top = np.argsort(-s)[:k]
        return [Item([self.turns[i].id], self.texts[i], float(s[i])) for i in top]
