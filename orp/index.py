"""BM25 index for the reference engine.

Same algorithm and parameters as the benchmark's shared BM25 (Okapi, k1=1.2, b=0.75,
idf = ln(1 + (N - df + 0.5)/(df + 0.5))). One addition for ORP §5.1/§5.10: when a
`mask` of admissible documents is given, N, df and the average length are computed
over admissible documents only, so inadmissible memories cannot influence scores
through collection statistics. With an all-true mask the scores equal the unmasked ones.
"""
from __future__ import annotations

from collections import Counter

import numpy as np
import scipy.sparse as sp

from ._text import terms


class BM25:
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
        self.n = len(docs)
        V = max(1, len(self.vocab))
        self.tf = sp.csc_matrix((np.array(vals, dtype=np.float32), (rows, cols)), shape=(self.n, V))
        self.lens = lens
        df = np.bincount(np.array(cols, dtype=np.int64), minlength=V).astype(np.float32)
        self.idf = np.log(1 + (self.n - df + 0.5) / (df + 0.5)).astype(np.float32)
        self.avg = float(lens.mean()) if self.n else 1.0
        self._norm = k1 * (1 - b + b * lens / max(self.avg, 1e-6))

    def score(self, q: str, weights: dict[str, float] | None = None, mask: np.ndarray | None = None) -> np.ndarray:
        out = np.zeros(self.n, dtype=np.float32)
        qt = Counter(terms(q)) if weights is None else weights
        if mask is not None and not mask.all():
            nm = int(mask.sum())
            if nm == 0:
                return out
            avg = float(self.lens[mask].mean()) or 1.0
            norm = self.k1 * (1 - self.b + self.b * self.lens / max(avg, 1e-6))
        else:
            mask, nm, norm = None, self.n, self._norm
        for t, qn in qt.items():
            j = self.vocab.get(t)
            if j is None:
                continue
            lo, hi = self.tf.indptr[j], self.tf.indptr[j + 1]
            rows, tfv = self.tf.indices[lo:hi], self.tf.data[lo:hi]
            if mask is not None:
                keep = mask[rows]
                rows, tfv = rows[keep], tfv[keep]
                df = len(rows)
                idf = np.log(1 + (nm - df + 0.5) / (df + 0.5))
            else:
                idf = self.idf[j]
            out[rows] += idf * tfv * (self.k1 + 1) / (tfv + norm[rows]) * qn
        return out

    def df(self, t: str, mask: np.ndarray | None = None) -> int:
        j = self.vocab.get(t)
        if j is None:
            return 0
        rows = self.tf.indices[self.tf.indptr[j]: self.tf.indptr[j + 1]]
        return int(mask[rows].sum()) if mask is not None else len(rows)
