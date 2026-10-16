// Column-major (CSC-style) store for the mostly-zero vectors of sparse encoders (feature hashing), with a batch dot
// product against one query that is bit-identical to f32.ts dot32 / dot32Sparse.
//
// Why it is exact: those dot products add float32 products (exact in float64) into four float64 partial sums, the
// partial sum of coordinate i being i & 3, in ascending coordinate order, and round once at the end. Adding a product
// that is zero changes nothing, so skipping zero entries gives the same sums. Here the outer loop runs over the query's
// non-zero coordinates in ascending order and scatters into per-row partial sums, so every row sees its terms in the
// same order as the row-wise loops do. Work is proportional to the stored non-zeros in the query's columns instead of
// rows x query non-zeros random reads, and memory is proportional to the non-zeros (about one tenth of a dense row).
import { f } from "./f32.js";

export class SparseCols {
  private rowIdx: Int32Array[];
  private val: Float32Array[];
  private len: Int32Array;
  n = 0;
  private acc = new Float64Array(0);
  private out = new Float64Array(0);

  constructor(readonly dim: number) {
    this.rowIdx = Array.from({ length: dim }, () => new Int32Array(0));
    this.val = Array.from({ length: dim }, () => new Float32Array(0));
    this.len = new Int32Array(dim);
  }

  /** Append a row (its non-zero coordinates; pass null for an all-zero row). Returns the row number. */
  add(v: Float32Array | null): number {
    const row = this.n++;
    if (v === null) return row;
    for (let d = 0; d < this.dim; d++) {
      const x = v[d]!;
      if (x === 0) continue;
      let l = this.len[d]!;
      let ri = this.rowIdx[d]!;
      if (l === ri.length) {
        const cap = Math.max(16, l * 2);
        const nr = new Int32Array(cap), nv = new Float32Array(cap);
        nr.set(ri);
        nv.set(this.val[d]!);
        this.rowIdx[d] = ri = nr;
        this.val[d] = nv;
      }
      ri[l] = row;
      this.val[d]![l] = x;
      this.len[d] = l + 1;
    }
    return row;
  }

  /** Dot product of every row with q, identical to dot32Sparse (nz given: the query's non-zero coordinates, ascending)
   * or dot32 (nz null), as float32 values in a Float64Array valid until the next call. */
  dotAll(q: Float32Array, nz: Int32Array | null): Float64Array {
    const n = this.n, D = this.dim;
    if (this.acc.length < 4 * n) this.acc = new Float64Array(4 * Math.max(n, 1024) + 4 * (n >> 1));
    if (this.out.length < n) this.out = new Float64Array(Math.max(n, 1024) + (n >> 1));
    const acc = this.acc, out = this.out;
    acc.fill(0, 0, 4 * n);
    const tail = D & ~3; // dot32 puts coordinates past the last full group of four in the first partial sum
    const visit = (i: number) => {
      const qi = q[i]!;
      const l = this.len[i]!;
      if (!l) return;
      const base = (nz ? i & 3 : i < tail ? i & 3 : 0) * n;
      const ri = this.rowIdx[i]!, vv = this.val[i]!;
      for (let e = 0; e < l; e++) acc[base + ri[e]!]! += vv[e]! * qi;
    };
    if (nz) for (let k = 0; k < nz.length; k++) visit(nz[k]!);
    else for (let i = 0; i < D; i++) if (q[i] !== 0) visit(i);
    const n1 = n, n2 = 2 * n, n3 = 3 * n;
    for (let j = 0; j < n; j++) out[j] = f((acc[j]! + acc[n1 + j]!) + (acc[n2 + j]! + acc[n3 + j]!));
    return out;
  }
}
