"""Train the shared dense encoder used by every system (PPMI-SVD word vectors + SIF).

Unsupervised and label-free, trained once on the union of benchmark text (all splits, including question strings),
i.e. transductive like fitting IDF on a corpus. No answers or evidence labels are used.

    python -m bench.train_encoder [out.pkl]

Writes to results/cache/dense_rebuilt.pkl by default (never over the shipped results/cache/dense.pkl, whose sha256
is in results/ENCODER.sha256 and which every published result used). The shipped file was built by this code before
the SVD start vector was seeded, so a rebuild is equivalent but not byte-identical: singular vectors agree up to
sign and numerical tolerance. The script reports how closely the rebuilt encoder's word-similarity structure
matches the shipped one (Pearson r over pairwise cosines of the 2,000 most frequent words).
"""
from __future__ import annotations

import hashlib
import os
import sys

import numpy as np

from .data import all_texts
from .encoders import CACHE_DIR, DenseEncoder


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CACHE_DIR, "dense_rebuilt.pkl")
    shipped = os.path.join(CACHE_DIR, "dense.pkl")
    if os.path.abspath(out) == os.path.abspath(shipped):
        sys.exit("refusing to overwrite the shipped encoder")
    enc = DenseEncoder.train(all_texts(), dim=256)
    enc.save(out)
    print(out, hashlib.sha256(open(out, "rb").read()).hexdigest())
    if os.path.exists(shipped):
        ref = DenseEncoder.load(shipped)
        inv = {i: w for w, i in ref.vocab.items()}
        words = [inv[i] for i in np.argsort(-ref.pw)[:2000]]
        A = np.stack([ref.vecs[ref.vocab[w]] for w in words if w in enc.vocab])
        B = np.stack([enc.vecs[enc.vocab[w]] for w in words if w in enc.vocab])
        iu = np.triu_indices(len(A), 1)
        r = np.corrcoef((A @ A.T)[iu], (B @ B.T)[iu])[0, 1]
        print(f"word-similarity agreement with the shipped encoder: r = {r:.4f} over {len(A)} words")


if __name__ == "__main__":
    main()
