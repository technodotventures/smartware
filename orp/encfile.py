"""The .orpenc encoder file: a data-only format for word-vector encoders (no pickle, nothing executable).

Layout (all little-endian):
    8 bytes   magic  b"ORPENC1\\n"
    4 bytes   uint32 header length H
    H bytes   UTF-8 JSON header: {"type": "sif", "dim": D, "vocab_size": V, "a": float, "name": str,
                                  "vocab": [V words, index order], "sha256": hex of the payload below}
    V*D*4     float32 word vectors, row-major
    V*4       float32 word probabilities p(w)
    D*4       float32 common component (SIF first principal component)

The Python oracle and the TypeScript engine read the same file, so both see bit-identical vectors.
"""
from __future__ import annotations

import hashlib
import json
import struct

import numpy as np

MAGIC = b"ORPENC1\n"


def write(path: str, vocab: dict[str, int], vecs: np.ndarray, pw: np.ndarray, pc: np.ndarray, a: float,
          name: str = "sif") -> str:
    words = [None] * len(vocab)
    for w, i in vocab.items():
        words[i] = w
    if any(w is None for w in words):
        raise ValueError("vocab indices must be 0..V-1 without gaps")
    V, D = vecs.shape
    payload = (np.ascontiguousarray(vecs, dtype="<f4").tobytes() + np.ascontiguousarray(pw, dtype="<f4").tobytes()
               + np.ascontiguousarray(pc, dtype="<f4").tobytes())
    sha = hashlib.sha256(payload).hexdigest()
    head = json.dumps({"type": "sif", "dim": D, "vocab_size": V, "a": float(a), "name": name, "vocab": words,
                       "sha256": sha}, ensure_ascii=False).encode()
    with open(path, "wb") as f:
        f.write(MAGIC + struct.pack("<I", len(head)) + head + payload)
    return sha


def read(path: str) -> tuple[dict, dict[str, int], np.ndarray, np.ndarray, np.ndarray]:
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != MAGIC:
        raise ValueError(f"{path}: not an .orpenc file")
    (h,) = struct.unpack("<I", data[8:12])
    head = json.loads(data[12:12 + h].decode())
    if head.get("type") != "sif":
        raise ValueError(f"{path}: unsupported encoder type {head.get('type')!r}")
    V, D = int(head["vocab_size"]), int(head["dim"])
    body = data[12 + h:]
    if len(body) != (V * D + V + D) * 4:
        raise ValueError(f"{path}: payload size mismatch")
    if hashlib.sha256(body).hexdigest() != head["sha256"]:
        raise ValueError(f"{path}: checksum mismatch")
    arr = np.frombuffer(body, dtype="<f4")
    vecs = arr[: V * D].reshape(V, D)
    pw = arr[V * D: V * D + V]
    pc = arr[V * D + V:]
    vocab = {w: i for i, w in enumerate(head["vocab"])}
    return head, vocab, vecs, pw, pc
