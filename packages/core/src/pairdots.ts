// Float32 dot products between two rows of one matrix as numpy computes them (elementwise float32 product, then
// pairwise summation, see f32.ts), and a bounded cache of them keyed by the pair of rows.
//
// The dense-window channel needs the dot product of each unit with its neighbours in the thread. Vectors never change
// once added, so each pair is computed once per partition. The product is commutative, so the pair is stored with the
// smaller row first and (k, next) and (next, k) share one entry.
import { f } from "./f32.js";

/** numpy's float32 dot of rows a and b of M (each n wide): pairwiseSum32 over the products, without materialising
 * them. Identical to filling a Float32Array with M[a+j]*M[b+j] and calling pairwiseSum32 on it. */
export function pairwiseDot32(M: Float32Array, ao: number, bo: number, n: number): number {
  if (n < 8) {
    let res = 0;
    for (let i = 0; i < n; i++) res = f(res + f(M[ao + i]! * M[bo + i]!));
    return res;
  }
  if (n <= 128) {
    let r0 = f(M[ao]! * M[bo]!), r1 = f(M[ao + 1]! * M[bo + 1]!), r2 = f(M[ao + 2]! * M[bo + 2]!);
    let r3 = f(M[ao + 3]! * M[bo + 3]!), r4 = f(M[ao + 4]! * M[bo + 4]!), r5 = f(M[ao + 5]! * M[bo + 5]!);
    let r6 = f(M[ao + 6]! * M[bo + 6]!), r7 = f(M[ao + 7]! * M[bo + 7]!);
    let i = 8;
    const end = n - (n % 8);
    for (; i < end; i += 8) {
      const x = ao + i, y = bo + i;
      r0 = f(r0 + f(M[x]! * M[y]!));
      r1 = f(r1 + f(M[x + 1]! * M[y + 1]!));
      r2 = f(r2 + f(M[x + 2]! * M[y + 2]!));
      r3 = f(r3 + f(M[x + 3]! * M[y + 3]!));
      r4 = f(r4 + f(M[x + 4]! * M[y + 4]!));
      r5 = f(r5 + f(M[x + 5]! * M[y + 5]!));
      r6 = f(r6 + f(M[x + 6]! * M[y + 6]!));
      r7 = f(r7 + f(M[x + 7]! * M[y + 7]!));
    }
    let res = f(f(f(r0 + r1) + f(r2 + r3)) + f(f(r4 + r5) + f(r6 + r7)));
    for (; i < n; i++) res = f(res + f(M[ao + i]! * M[bo + i]!));
    return res;
  }
  let n2 = Math.floor(n / 2);
  n2 -= n2 % 8;
  return f(pairwiseDot32(M, ao, bo, n2) + pairwiseDot32(M, ao + n2, bo + n2, n - n2));
}

/** Open-addressing table (linear probing) from a pair of non-negative int32 row numbers to a float32. Bounded: once
 * `max` entries are stored, further pairs are computed but not kept. */
export class PairDots {
  private a = new Int32Array(1 << 12).fill(-1);
  private b = new Int32Array(1 << 12);
  private v = new Float32Array(1 << 12);
  private mask = (1 << 12) - 1;
  size = 0;

  constructor(private readonly max = 4_000_000) {}

  private slot(x: number, y: number): number {
    return (Math.imul(x, 0x9e3779b1) ^ Math.imul(y, 0x85ebca6b) ^ (x >>> 15)) & this.mask;
  }

  /** The dot product of rows x and y of M (width d), cached. */
  get(M: Float32Array, d: number, x: number, y: number): number {
    if (x > y) { const t = x; x = y; y = t; }
    const a = this.a, b = this.b;
    let i = this.slot(x, y);
    for (;;) {
      const ax = a[i]!;
      if (ax === -1) break;
      if (ax === x && b[i] === y) return this.v[i]!;
      i = (i + 1) & this.mask;
    }
    const val = pairwiseDot32(M, x * d, y * d, d);
    if (this.size < this.max) {
      if ((this.size + 1) * 2 > this.mask) this.grow();
      this.put(x, y, val);
    }
    return val;
  }

  private put(x: number, y: number, val: number): void {
    let i = this.slot(x, y);
    while (this.a[i] !== -1) i = (i + 1) & this.mask;
    this.a[i] = x;
    this.b[i] = y;
    this.v[i] = val;
    this.size++;
  }

  private grow(): void {
    const oa = this.a, ob = this.b, ov = this.v;
    const cap = (this.mask + 1) * 2;
    this.a = new Int32Array(cap).fill(-1);
    this.b = new Int32Array(cap);
    this.v = new Float32Array(cap);
    this.mask = cap - 1;
    this.size = 0;
    for (let i = 0; i < oa.length; i++) if (oa[i] !== -1) this.put(oa[i]!, ob[i]!, ov[i]!);
  }
}
