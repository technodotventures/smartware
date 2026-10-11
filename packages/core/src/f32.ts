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
  let s = 0;
  for (let i = 0; i < n; i++) s += a[ao + i]! * b[bo + i]!;
  return f(s);
}
