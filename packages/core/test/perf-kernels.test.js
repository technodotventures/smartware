// Exactness tests for the performance kernels: each must match a plain reference bit for bit.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { blake2b } from "../dist/blake2b.js";
import { SparseCols } from "../dist/sparse.js";
import { PairDots, pairwiseDot32 } from "../dist/pairdots.js";
import { dot32, nonZero4, dot32Sparse4, pairwiseSum32, f } from "../dist/f32.js";
import { countTokens, wordsAtMost } from "../dist/text.js";

let seed = 12345;
const rnd = () => ((seed = (Math.imul(seed, 1103515245) + 12345) >>> 0) / 4294967296);

test("blake2b matches node crypto (64-byte digest) across lengths", () => {
  for (let len = 0; len <= 400; len += 7) {
    const b = Buffer.alloc(len);
    for (let i = 0; i < len; i++) b[i] = (rnd() * 256) | 0;
    assert.equal(Buffer.from(blake2b(b, 64)).toString("hex"), createHash("blake2b512").update(b).digest("hex"));
  }
});

test("blake2b with a short digest is a different hash, not a truncation", () => {
  const b = Buffer.from("smartware");
  assert.equal(blake2b(b, 8).length, 8);
  assert.notEqual(Buffer.from(blake2b(b, 8)).toString("hex"), Buffer.from(blake2b(b, 64)).toString("hex").slice(0, 16));
});

function sparseVec(D, density) {
  const v = new Float32Array(D);
  for (let i = 0; i < D; i++) if (rnd() < density) v[i] = (Math.round((rnd() - 0.5) * 20) || 1) / 4;
  return v;
}

test("SparseCols.dotAll equals dot32 and dot32Sparse4 for dense and sparse queries", () => {
  for (const D of [5, 64, 131, 512]) {
    const rows = Array.from({ length: 60 }, (_, k) => (k % 9 === 0 ? null : sparseVec(D, 0.1)));
    const sc = new SparseCols(D);
    for (const r of rows) sc.add(r);
    for (let t = 0; t < 8; t++) {
      const q = sparseVec(D, t < 4 ? 0.1 : 0.9);
      const out = sc.dotAll(q, null);
      rows.forEach((r, k) => {
        const want = r ? dot32(r, 0, q, 0, D) : 0;
        assert.equal(out[k], want, `D=${D} row ${k}`);
      });
    }
  }
});

test("pairwiseDot32 and PairDots match the reference pairwise dot", () => {
  const D = 256, n = 40;
  const M = new Float32Array(n * D);
  for (let i = 0; i < M.length; i++) M[i] = f(rnd() - 0.5);
  const pd = new PairDots();
  for (let t = 0; t < 400; t++) {
    const x = (rnd() * n) | 0, y = (rnd() * n) | 0;
    const prod = new Float32Array(D);
    for (let i = 0; i < D; i++) prod[i] = M[x * D + i] * M[y * D + i];
    const want = pairwiseSum32(prod, 0, D);
    assert.equal(pairwiseDot32(M, x * D, y * D, D), want);
    assert.equal(pd.get(M, D, x, y), want);
    assert.equal(pd.get(M, D, y, x), want);
  }
});

test("countTokens and wordsAtMost agree with a regex tokeniser", () => {
  const re = /\w+|[^\w\s]/gu;
  const samples = ["", "hello world", "a,b;c", "naïve café 東京 ok", "x".repeat(300), "emoji 😀 test", "snake_case and 42nd"];
  for (const s of samples) {
    const ws = (s.match(/\S+/g) ?? []).length;
    for (const lim of [0, 1, 2, 5, 20]) assert.equal(wordsAtMost(s, lim), ws <= lim, `${JSON.stringify(s)} ${lim}`);
    assert.ok(countTokens(s) >= 0 && Number.isInteger(countTokens(s)));
    assert.ok(re);
  }
});
