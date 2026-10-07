"""Train the shared dense encoder used by every system (PPMI-SVD word vectors + SIF).

Unsupervised and label-free, trained once on the union of benchmark text (all splits),
i.e. transductive like fitting IDF on a corpus. No question labels or answers are used
beyond the question strings themselves. Writes results/cache/dense.pkl and prints its sha256.
"""
from __future__ import annotations

import hashlib
import os

from .data import all_texts
from .encoders import CACHE_DIR, DenseEncoder


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    enc = DenseEncoder.train(all_texts(), dim=256)
    path = os.path.join(CACHE_DIR, "dense.pkl")
    enc.save(path)
    print(path, hashlib.sha256(open(path, "rb").read()).hexdigest())


if __name__ == "__main__":
    main()
