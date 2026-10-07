"""Shared encoders for the benchmark.

BM25: classic Okapi BM25 (k1=1.2, b=0.75) over stemmed, stopword-free terms.
Dense: a fixed general-purpose embedder trained once, offline, on the union of
benchmark text (PPMI co-occurrence -> truncated SVD word vectors -> SIF sentence
embedding with first principal component removed; Arora et al. 2017). No neural
model weights can be downloaded in this sandbox, so this stands in for an
off-the-shelf sentence embedder. Every system gets the *same* encoder, so the
comparison isolates memory architecture.
"""
from __future__ import annotations

import hashlib
import math
import os
import pickle
from collections import Counter

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import svds

from .text import STOPWORDS, terms, words

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "results", "cache")


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


class DenseEncoder:
    """Fixed sentence embedder (PPMI-SVD + SIF). Call DenseEncoder.load()."""

    def __init__(self, vocab: dict[str, int], vecs: np.ndarray, pw: np.ndarray, pc: np.ndarray, a: float = 1e-3):
        self.vocab, self.vecs, self.pw, self.pc, self.a = vocab, vecs, pw, pc, a
        self.dim = vecs.shape[1]
        self._cache: dict[str, np.ndarray] = {}

    # -- training -------------------------------------------------------
    @staticmethod
    def train(texts: list[str], dim: int = 256, window: int = 5, min_count: int = 3) -> "DenseEncoder":
        toks = [words(t) for t in texts]
        cnt = Counter(w for ts in toks for w in ts)
        vocab = {w: i for i, (w, c) in enumerate(cnt.most_common()) if c >= min_count}
        V = len(vocab)
        rows, cols, vals = [], [], []
        for ts in toks:
            ids = [vocab.get(w, -1) for w in ts]
            for i, wi in enumerate(ids):
                if wi < 0:
                    continue
                for d in range(1, window + 1):
                    if i + d >= len(ids):
                        break
                    wj = ids[i + d]
                    if wj < 0:
                        continue
                    wt = 1.0 / d
                    rows += [wi, wj]
                    cols += [wj, wi]
                    vals += [wt, wt]
        C = sp.coo_matrix((np.array(vals, np.float32), (rows, cols)), shape=(V, V)).tocsr()
        C.sum_duplicates()
        total = C.sum()
        rs = np.asarray(C.sum(axis=1)).ravel()
        cs = np.asarray(C.sum(axis=0)).ravel() ** 0.75
        cs = cs / cs.sum()
        C = C.tocoo()
        pmi = np.log((C.data / total) / ((rs[C.row] / total) * cs[C.col]) + 1e-12)
        keep = pmi > 0
        P = sp.csr_matrix((pmi[keep].astype(np.float32), (C.row[keep], C.col[keep])), shape=(V, V))
        U, S, _ = svds(P, k=dim)
        W = U * np.sqrt(S)
        W /= np.linalg.norm(W, axis=1, keepdims=True) + 1e-9
        freq = np.array([cnt[w] for w in vocab], dtype=np.float64)
        pw = (freq / freq.sum()).astype(np.float32)
        enc = DenseEncoder(vocab, W.astype(np.float32), pw, np.zeros(dim, np.float32))
        # common component removal (SIF)
        sample = [t for t in texts[:: max(1, len(texts) // 20000)]]
        M = np.stack([enc._raw(t) for t in sample])
        M = M[np.linalg.norm(M, axis=1) > 0]
        _, _, vt = np.linalg.svd(M - 0, full_matrices=False)
        enc.pc = vt[0].astype(np.float32)
        return enc

    def _raw(self, text: str) -> np.ndarray:
        ids = [self.vocab[w] for w in words(text) if w in self.vocab]
        if not ids:
            return np.zeros(self.dim, np.float32)
        ids = np.array(ids)
        wts = self.a / (self.a + self.pw[ids])
        return (self.vecs[ids] * wts[:, None]).sum(0) / len(ids)

    def embed(self, text: str) -> np.ndarray:
        v = self._cache.get(text)
        if v is None:
            r = self._raw(text)
            r = r - self.pc * float(r @ self.pc)
            n = float(np.linalg.norm(r))
            v = (r / n) if n > 0 else r
            if len(self._cache) < 2_000_000:
                self._cache[text] = v
        return v

    def embed_many(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        return np.stack([self.embed(t) for t in texts])

    def save(self, path: str):
        with open(path, "wb") as f:
            pickle.dump((self.vocab, self.vecs, self.pw, self.pc, self.a), f)

    @staticmethod
    def load(path: str | None = None) -> "DenseEncoder":
        path = path or os.path.join(CACHE_DIR, "dense.pkl")
        with open(path, "rb") as f:
            return DenseEncoder(*pickle.load(f))


class Encoders:
    """Bundle handed to every system."""

    def __init__(self, dense: DenseEncoder):
        self.dense = dense

    def bm25(self, docs: list[str], **kw) -> BM25:
        return BM25(docs, **kw)

    def embed(self, text: str) -> np.ndarray:
        return self.dense.embed(text)

    def embed_many(self, texts: list[str]) -> np.ndarray:
        return self.dense.embed_many(texts)
