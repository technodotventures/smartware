// BM25 (Okapi, k1 = 1.2, b = 0.75, idf = ln(1 + (N - df + 0.5) / (df + 0.5))), port of smartware/index.py.
// Scores are always computed over an admissibility mask: N, document frequency and average length come from
// admissible documents only, so a store and the same store with inadmissible documents removed score identically
// (M33, M67). Incremental: documents are appended; postings stay in ascending document order.
import { f, mean32 } from "./f32.js";
import { counter, terms } from "./text.js";

const K1 = 1.2, B = 0.75;
const K1_32 = f(K1), ONE_MINUS_B = f(1 - B), B_32 = f(B);

export type Mask = Uint8Array;

export class BM25 {
  readonly vocab = new Map<string, number>();
  private postDoc: number[][] = [];
  private postTf: number[][] = [];
  lens: number[] = [];

  get n(): number {
    return this.lens.length;
  }

  add(doc: string): number {
    const k = this.lens.length;
    const c = counter(terms(doc));
    let len = 0;
    for (const [t, n] of c) {
      len += n;
      let j = this.vocab.get(t);
      if (j === undefined) {
        j = this.vocab.size;
        this.vocab.set(t, j);
        this.postDoc.push([]);
        this.postTf.push([]);
      }
      this.postDoc[j]!.push(k);
      this.postTf[j]!.push(n);
    }
    this.lens.push(f(len));
    return k;
  }

  // scratch reused across calls: per-document length normalisation (NaN = not computed yet) and the admissible lengths
  private normBuf = new Float32Array(0);
  private lenBuf = new Float32Array(0);
  private allAvgN = -1;
  private allAvg = 0;

  /** Score every document; documents outside the mask score 0. `weights` replaces the query's own term counts
   * (used by pseudo-relevance feedback), in the given insertion order. */
  score(q: string, mask: Mask, weights?: Map<string, number>): Float32Array {
    const n = this.n;
    const out = new Float32Array(n);
    const qt = weights ?? counter(terms(q));
    let nm = 0;
    for (let k = 0; k < n; k++) nm += mask[k]!;
    if (nm === 0) return out;
    const all = nm === n; // every document admissible: no mask tests, document frequency is the posting length
    let avg: number;
    if (all) {
      if (this.allAvgN !== n) { this.allAvg = mean32(this.lens) || 1.0; this.allAvgN = n; }
      avg = this.allAvg;
    } else {
      if (this.lenBuf.length < nm) this.lenBuf = new Float32Array(nm + (nm >> 1));
      let c = 0;
      for (let k = 0; k < n; k++) if (mask[k]) this.lenBuf[c++] = this.lens[k]!;
      avg = mean32(this.lenBuf, 0, nm) || 1.0;
    }
    const avgDen = Math.max(avg, 1e-6);
    if (this.normBuf.length < n) this.normBuf = new Float32Array(n + (n >> 1));
    const normBuf = this.normBuf, lens = this.lens;
    normBuf.fill(NaN, 0, n);
    for (const [t, qn] of qt) {
      const j = this.vocab.get(t);
      if (j === undefined) continue;
      const docs = this.postDoc[j]!, tfs = this.postTf[j]!;
      const m = docs.length;
      let df = m;
      if (!all) {
        df = 0;
        for (let i = 0; i < m; i++) if (mask[docs[i]!]) df++;
      }
      const idf = Math.log(1 + (nm - df + 0.5) / (df + 0.5));
      for (let i = 0; i < m; i++) {
        const k = docs[i]!;
        if (!all && !mask[k]) continue;
        const tf = tfs[i]!;
        let nv = normBuf[k]!;
        if (nv !== nv) {
          nv = f(K1_32 * f(ONE_MINUS_B + f(f(B_32 * lens[k]!) / avgDen)));
          normBuf[k] = nv;
        }
        out[k] = out[k]! + idf * tf * (K1 + 1) / f(tf + nv) * qn;
      }
    }
    return out;
  }

  df(t: string, mask: Mask): number {
    const j = this.vocab.get(t);
    if (j === undefined) return 0;
    let c = 0;
    for (const k of this.postDoc[j]!) if (mask[k]) c++;
    return c;
  }
}
