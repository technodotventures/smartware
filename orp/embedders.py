"""Embedders for the reference servers.

ORP does not standardise embeddings (they are engine internals, spec §3.1). The
reference engine accepts anything with embed()/embed_many(). Two built-ins:

- HashingEmbedder: feature-hashed words and word bigrams. No weights, no network,
  deterministic. Lexical-ish; good for tests and air-gapped installs.
- SIFEmbedder: loads PPMI-SVD word vectors (the benchmark's encoder) and builds
  SIF sentence embeddings. Swap in any neural embedder in production.
"""
from __future__ import annotations

import hashlib
import pickle
import re

import numpy as np

_W = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


class HashingEmbedder:
    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed(self, text: str) -> np.ndarray:
        ws = _W.findall(text.lower())
        v = np.zeros(self.dim, np.float32)
        for tok in ws + [a + "_" + b for a, b in zip(ws, ws[1:])]:
            h = int(hashlib.blake2b(tok.encode(), digest_size=8).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def embed_many(self, texts):
        return np.stack([self.embed(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)


class SIFEmbedder:
    def __init__(self, path: str, a: float = 1e-3):
        vocab, vecs, pw, pc, a0 = pickle.load(open(path, "rb"))
        self.vocab, self.vecs, self.pw, self.pc, self.a = vocab, vecs, pw, pc, a0 or a
        self.dim = vecs.shape[1]
        self._cache = {}

    def embed(self, text: str) -> np.ndarray:
        v = self._cache.get(text)
        if v is not None:
            return v
        ids = [self.vocab[w] for w in _W.findall(text.lower()) if w in self.vocab]
        if not ids:
            r = np.zeros(self.dim, np.float32)
        else:
            ids = np.array(ids)
            wts = self.a / (self.a + self.pw[ids])
            r = (self.vecs[ids] * wts[:, None]).sum(0) / len(ids)
            r = r - self.pc * float(r @ self.pc)
            n = float(np.linalg.norm(r))
            r = r / n if n else r
        if len(self._cache) < 500_000:
            self._cache[text] = r
        return r

    def embed_many(self, texts):
        return np.stack([self.embed(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)


def load(spec: str | None):
    if not spec or spec == "hashing":
        return HashingEmbedder()
    if spec.startswith("sif:"):
        return SIFEmbedder(spec[4:])
    raise ValueError(f"unknown embedder {spec!r}")
