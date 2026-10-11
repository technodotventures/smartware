"""Convert the benchmark's pickled encoder (results/cache/dense.pkl, which you created) to the data-only .swenc format.

    SMARTWARE_ALLOW_PICKLE=1 python3 tools/export_encoder.py results/cache/dense.pkl results/cache/sif-ppmi-256.swenc
"""
import pickle
import sys

from smartware import encfile

src, dst = sys.argv[1], sys.argv[2]
with open(src, "rb") as f:
    vocab, vecs, pw, pc, a = pickle.load(f)
sha = encfile.write(dst, vocab, vecs, pw, pc, a, name="sif-ppmi-256")
print(dst, sha)
