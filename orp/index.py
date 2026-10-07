"""BM25 index used by the reference engine (identical algorithm and parameters to the
benchmark's shared BM25, so comparisons stay apples to apples)."""
from __future__ import annotations

from collections import Counter

import numpy as np
import scipy.sparse as sp

from ._text import terms


class BM25:
    """Sparse BM25 index over a list of documents. score(query) -> np.ndarray of len(docs)."""

    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.vocab: dict[str, int] = {}
        rows, cols, vals = [], [], []
        lens = np.zeros(len(docs), dtype=np.float32)
        for i, d in enumerate(docs):
            c = Counter(terms(d))
            lens[i] = sum(c.values())
            for t, n in c.items():
                j = self.vocab.setdefault(t, len(self.vocab))
                rows.append(i)
                cols.append(j)
                vals.append(n)
        n_docs = max(1, len(docs))
        tf = sp.csr_matrix((np.array(vals, dtype=np.float32), (rows, cols)), shape=(len(docs), max(1, len(self.vocab))))
        df = np.bincount(np.array(cols, dtype=np.int64), minlength=max(1, len(self.vocab))).astype(np.float32)
        self.idf = np.log(1 + (n_docs - df + 0.5) / (df + 0.5)).astype(np.float32)
        avg = float(lens.mean()) if len(docs) else 1.0
        norm = k1 * (1 - b + b * lens / max(avg, 1e-6))
        tf = tf.tocoo()
        w = tf.data * (k1 + 1) / (tf.data + norm[tf.row])
        self.mat = sp.csc_matrix((w.astype(np.float32), (tf.row, tf.col)), shape=tf.shape)
        self.n = len(docs)

    def query_terms(self, q: str) -> list[int]:
        return [self.vocab[t] for t in terms(q) if t in self.vocab]

    def score(self, q: str, weights: dict[str, float] | None = None) -> np.ndarray:
        out = np.zeros(self.n, dtype=np.float32)
        if weights is None:
            qt = Counter(terms(q))
        else:
            qt = weights
        for t, qn in qt.items():
            j = self.vocab.get(t)
            if j is None:
                continue
            col = self.mat.getcol(j)
            out[col.indices] += self.idf[j] * col.data * qn
        return out
