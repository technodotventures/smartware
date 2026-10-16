// Float32 arithmetic as numpy does it. The oracle keeps BM25 scores, lengths and embeddings in float32, so to rank
// identically the product engine rounds at the same steps. Float32 +,-,*,/ and sqrt computed in float64 and then
// rounded once are exact (float64 carries more than 2*24+2 bits), so Math.fround after each op reproduces numpy.

export const f = Math.fround;

/** numpy's pairwise summation for float32 (umath loops: unroll by 8, block size 128). */
export function pairwiseSum32(a: ArrayLike<number>, off: number, n: number): number {
  if (n < 8) {
    let res = 0;
    for (let i = 0; i < n; i++) res = f(res + a[off + i]!);
    return res;
  }
  if (n <= 128) {
    const r = [a[off]!, a[off + 1]!, a[off + 2]!, a[off + 3]!, a[off + 4]!, a[off + 5]!, a[off + 6]!, a[off + 7]!]
      .map(f);
    let i = 8;
    for (; i < n - (n % 8); i += 8) {
      for (let j = 0; j < 8; j++) r[j] = f(r[j]! + a[off + i + j]!);
    }
    let res = f(f(f(r[0]! + r[1]!) + f(r[2]! + r[3]!)) + f(f(r[4]! + r[5]!) + f(r[6]! + r[7]!)));
    for (; i < n; i++) res = f(res + a[off + i]!);
    return res;
  }
  let n2 = Math.floor(n / 2);
  n2 -= n2 % 8;
  return f(pairwiseSum32(a, off, n2) + pairwiseSum32(a, off + n2, n - n2));
}

/** np.add.reduce over a contiguous float32 array (the reduction starts from the identity 0). */
export function sum32(a: ArrayLike<number>, off = 0, n = a.length - off): number {
  return f(0 + pairwiseSum32(a, off, n));
}

/** np.mean for float32: float32 sum divided by the count in float32. */
export function mean32(a: ArrayLike<number>, off = 0, n = a.length - off): number {
  return f(sum32(a, off, n) / n);
}

/** A float32 dot product. BLAS accumulation order is not specified, so this accumulates in float64 and rounds
 * once; it agrees with numpy to within an ulp, which matters only for scores that are already near-ties. */
export function dot32(a: Float32Array, ao: number, b: Float32Array, bo: number, n: number): number {
  // four float64 partial sums: float32 products are exact in float64, and the sum differs from a sequential one only
  // at the 1e-16 level, far below the single float32 rounding at the end
  let s0 = 0, s1 = 0, s2 = 0, s3 = 0, i = 0;
  for (; i + 3 < n; i += 4) {
    s0 += a[ao + i]! * b[bo + i]!;
    s1 += a[ao + i + 1]! * b[bo + i + 1]!;
    s2 += a[ao + i + 2]! * b[bo + i + 2]!;
    s3 += a[ao + i + 3]! * b[bo + i + 3]!;
  }
  for (; i < n; i++) s0 += a[ao + i]! * b[bo + i]!;
  return f((s0 + s1) + (s2 + s3));
}

/** Non-zero coordinates of a query vector, for sparse dot products (hashing vectors are mostly zeros). */
export function nonZero(v: Float32Array): Int32Array | null {
  let n = 0;
  for (let i = 0; i < v.length; i++) if (v[i] !== 0) n++;
  if (n * 4 > v.length) return null; // dense enough: the plain loop is faster
  const out = new Int32Array(n);
  let k = 0;
  for (let i = 0; i < v.length; i++) if (v[i] !== 0) out[k++] = i;
  return out;
}

/** dot32 over the query's non-zero coordinates only, with each term in the same partial sum (index mod 4) and the
 * same order as dot32, so the result is bit-identical (the skipped terms add exact zeros). */
export function dot32Sparse(a: Float32Array, ao: number, q: Float32Array, nz: Int32Array): number {
  let s0 = 0, s1 = 0, s2 = 0, s3 = 0;
  for (let k = 0; k < nz.length; k++) {
    const i = nz[k]!;
    const t = a[ao + i]! * q[i]!;
    switch (i & 3) { case 0: s0 += t; break; case 1: s1 += t; break; case 2: s2 += t; break; default: s3 += t; }
  }
  return f((s0 + s1) + (s2 + s3));
}

/** The query's non-zero coordinates split by index mod 4, each list ascending (null when the query is dense). With a
 * list per partial sum, dot32Sparse4 has no data-dependent branch in its loop. */
export function nonZero4(v: Float32Array): Int32Array[] | null {
  let n = 0;
  for (let i = 0; i < v.length; i++) if (v[i] !== 0) n++;
  if (n * 4 > v.length) return null;
  const c = [0, 0, 0, 0];
  for (let i = 0; i < v.length; i++) if (v[i] !== 0) c[i & 3]!++;
  const out = [new Int32Array(c[0]!), new Int32Array(c[1]!), new Int32Array(c[2]!), new Int32Array(c[3]!)];
  const w = [0, 0, 0, 0];
  for (let i = 0; i < v.length; i++) if (v[i] !== 0) out[i & 3]![w[i & 3]!++] = i;
  return out;
}

/** dot32Sparse with the coordinates pre-split by residue: the same four partial sums, each accumulated over its own
 * coordinates in ascending order, so the result is bit-identical. */
export function dot32Sparse4(a: Float32Array, ao: number, q: Float32Array, z: Int32Array[]): number {
  let s0 = 0, s1 = 0, s2 = 0, s3 = 0;
  const z0 = z[0]!, z1 = z[1]!, z2 = z[2]!, z3 = z[3]!;
  for (let k = 0; k < z0.length; k++) { const i = z0[k]!; s0 += a[ao + i]! * q[i]!; }
  for (let k = 0; k < z1.length; k++) { const i = z1[k]!; s1 += a[ao + i]! * q[i]!; }
  for (let k = 0; k < z2.length; k++) { const i = z2[k]!; s2 += a[ao + i]! * q[i]!; }
  for (let k = 0; k < z3.length; k++) { const i = z3[k]!; s3 += a[ao + i]! * q[i]!; }
  return f((s0 + s1) + (s2 + s3));
}
