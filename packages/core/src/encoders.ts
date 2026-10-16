// Encoders (the engine's Encoder port). Smartware does not standardise embeddings (spec §3.1); an engine declares the one
// it uses. Built in:
//   hashing  feature-hashed words and word bigrams (BLAKE2b-64), no weights, no network, deterministic.
//   sif      PPMI-SVD word vectors + SIF sentence embedding, read from a data-only .swenc file.
//   openai   any OpenAI-compatible /embeddings endpoint; the key comes from the environment, never from a file.
// Recall itself is synchronous, so remote encoders are warmed asynchronously before the engine runs.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { blake2b, blake2b8, digest8 } from "./blake2b.js";
import { f } from "./f32.js";
import { words } from "./text.js";

export interface Encoder {
  readonly name: string;
  readonly dim: number;
  /** True when vectors are mostly zeros (feature hashing): the engine then keeps segment vectors in a column-major
   * sparse store and scores them with an identical-result batch dot product. Leave unset for dense embeddings. */
  readonly sparse?: boolean;
  embed(text: string): Float32Array;
  /** Prepare embeddings for texts before a synchronous embed() (remote encoders). */
  warm?(texts: string[]): Promise<void>;
  /** Drop cached embeddings of forgotten content. */
  forget?(content: string): void;
  /** For encoders that must be warmed: whether a vector for this text is ready. */
  has?(text: string): boolean;
  /** Drop cached and persisted vectors of these exact texts (purge and redact). */
  forgetTexts?(texts: string[]): void;
}

/** Optional persistent vector cache (the store's vectors table), so restarts do not re-embed. */
export interface VectorCache {
  get(encoder: string, sha: string): Float32Array | null | Promise<Float32Array | null>;
  put(encoder: string, sha: string, v: Float32Array): void | Promise<void>;
  delete?(encoder: string, sha: string): void;
}

const enc = new TextEncoder();

export class HashingEncoder implements Encoder {
  readonly name: string;
  readonly sparse = true;
  private readonly pow2: boolean;
  private readonly mask: number;
  constructor(readonly dim = 512) {
    this.name = `hashing-${dim}`;
    this.pow2 = (dim & (dim - 1)) === 0 && dim <= 1 << 24;
    this.mask = dim - 1;
  }

  // Word ids and their signed buckets (bucket + 1, negated for -1). The vocabulary is bounded, so this stays small.
  // Bigrams go through a fixed-size direct-mapped cache keyed by the pair of word ids: windows of neighbouring
  // sentences and the unit itself share most of their bigrams, so one BLAKE2b serves about three embeddings, while
  // the memory stays constant (an unbounded bigram map cost more memory and lookups than the hash it saved).
  private wordId = new Map<string, number>();
  private wordBucket = new Int32Array(1024);
  private static readonly PAIR_BITS = 17;
  private pairA = new Int32Array(1 << HashingEncoder.PAIR_BITS).fill(-1);
  private pairB = new Int32Array(1 << HashingEncoder.PAIR_BITS);
  private pairV = new Int32Array(1 << HashingEncoder.PAIR_BITS);
  private readonly bytes = new Uint8Array(1024);

  /** Signed bucket of the digest left in digest8 (Python: int(hexdigest, 16) % dim, sign from the first byte). */
  private signed(): number {
    const lo = digest8[0]!, hi = digest8[1]!;
    let idx: number;
    if (this.pow2) {
      // power-of-two dim: h % dim is the low bits of the big-endian integer of bytes 4..7 (a byte swap of hi)
      idx = ((((hi & 0xff) << 24) | ((hi & 0xff00) << 8) | ((hi >>> 8) & 0xff00) | (hi >>> 24)) >>> 0) & this.mask;
    } else {
      let h = 0n;
      for (let i = 0; i < 8; i++) h = (h << 8n) | BigInt(i < 4 ? (lo >>> (8 * i)) & 0xff : (hi >>> (8 * (i - 4))) & 0xff);
      idx = Number(h % BigInt(this.dim));
    }
    return (lo & 0x80) ? idx + 1 : -(idx + 1);
  }

  /** Write the ASCII string a + sep + b into the byte buffer; the length, or -1 if it does not fit or is not ASCII. */
  private put(a: string, sep: boolean, b: string): number {
    const buf = this.bytes;
    let n = 0;
    if (a.length + b.length + 1 > buf.length) return -1;
    for (let i = 0; i < a.length; i++) {
      const c = a.charCodeAt(i);
      if (c > 127) return -1;
      buf[n++] = c;
    }
    if (sep) {
      buf[n++] = 95; // "_"
      for (let i = 0; i < b.length; i++) {
        const c = b.charCodeAt(i);
        if (c > 127) return -1;
        buf[n++] = c;
      }
    }
    return n;
  }

  private hashOf(a: string, sep: boolean, b: string): number {
    const n = this.put(a, sep, b);
    if (n >= 0) blake2b8(this.bytes, n);
    else {
      const d = blake2b(enc.encode(sep ? a + "_" + b : a), 8);
      digest8[0] = d[0]! | (d[1]! << 8) | (d[2]! << 16) | (d[3]! << 24);
      digest8[1] = d[4]! | (d[5]! << 8) | (d[6]! << 16) | (d[7]! << 24);
    }
    return this.signed();
  }

  private static readonly MAX_WORDS = 2_000_000;

  private resetCache(): void {
    this.wordId.clear();
    this.pairA.fill(-1);
  }

  /** Id of a word, hashing it the first time it is seen. */
  private idOf(tok: string): number {
    let id = this.wordId.get(tok);
    if (id === undefined) {
      id = this.wordId.size;
      if (id >= this.wordBucket.length) {
        const g = new Int32Array(this.wordBucket.length * 2);
        g.set(this.wordBucket);
        this.wordBucket = g;
      }
      this.wordBucket[id] = this.hashOf(tok, false, "");
      this.wordId.set(tok, id);
    }
    return id;
  }

  embed(text: string): Float32Array {
    const ws = words(text);
    const n = ws.length;
    if (this.wordId.size + n > HashingEncoder.MAX_WORDS) this.resetCache(); // never mid-text: ids stay consistent
    const v = new Float32Array(this.dim);
    const shift = 32 - HashingEncoder.PAIR_BITS;
    let prev = -1;
    // Unigram and bigram contributions are +-1 added to integer-valued float32 counters, which are exact in any
    // order, so interleaving them gives the same vector as all unigrams followed by all bigrams.
    for (let i = 0; i < n; i++) {
      const id = this.idOf(ws[i]!);
      const b = this.wordBucket[id]!;
      if (b > 0) v[b - 1]! += 1.0; else v[-b - 1]! -= 1.0;
      if (i > 0) {
        const slot = (Math.imul(prev, 0x9e3779b1) ^ Math.imul(id, 0x85ebca6b)) >>> shift;
        let pb: number;
        if (this.pairA[slot] === prev && this.pairB[slot] === id) pb = this.pairV[slot]!;
        else {
          pb = this.hashOf(ws[i - 1]!, true, ws[i]!);
          this.pairA[slot] = prev;
          this.pairB[slot] = id;
          this.pairV[slot] = pb;
        }
        if (pb > 0) v[pb - 1]! += 1.0; else v[-pb - 1]! -= 1.0;
      }
      prev = id;
    }
    let ss = 0;
    for (let i = 0; i < v.length; i++) ss += v[i]! * v[i]!;
    const nn = f(Math.sqrt(f(ss)));
    if (nn) for (let i = 0; i < v.length; i++) v[i] = f(v[i]! / nn);
    return v;
  }
}

const MAGIC = "SWENC01\n";
const LEGACY_MAGIC = "O" + "RPENC1\n"; // the format's first name; same layout, still read
const sha = (t: string) => createHash("sha256").update(t).digest("hex");

export class SifEncoder implements Encoder {
  readonly name: string;
  readonly dim: number;
  private vocab = new Map<string, number>();
  private vecs: Float32Array;
  private pw: Float32Array;
  private pc: Float32Array;
  private a: number;
  private cache = new Map<string, Float32Array>();
  private wrows: Array<Float32Array | undefined> = [];

  private weighted(i: number): Float32Array {
    let w = this.wrows[i];
    if (!w) {
      const D = this.dim, o = i * D;
      const wt = f(this.a / f(this.a + this.pw[i]!));
      w = new Float32Array(D);
      for (let j = 0; j < D; j++) w[j] = this.vecs[o + j]! * wt; // stored as float32: f(vec * wt)
      this.wrows[i] = w;
    }
    return w;
  }

  constructor(path: string) {
    const data = readFileSync(path);
    const magic = data.subarray(0, 8).toString("latin1");
    if (magic !== MAGIC && magic !== LEGACY_MAGIC) throw new Error(`${path}: not an .swenc file`);
    const h = data.readUInt32LE(8);
    const head = JSON.parse(data.subarray(12, 12 + h).toString("utf8"));
    if (head.type !== "sif") throw new Error(`${path}: unsupported encoder type ${head.type}`);
    const V = head.vocab_size as number, D = head.dim as number;
    const body = data.subarray(12 + h);
    if (body.length !== (V * D + V + D) * 4) throw new Error(`${path}: payload size mismatch`);
    if (createHash("sha256").update(body).digest("hex") !== head.sha256) throw new Error(`${path}: checksum mismatch`);
    const arr = new Float32Array(body.buffer.slice(body.byteOffset, body.byteOffset + body.length));
    this.vecs = arr.subarray(0, V * D);
    this.pw = arr.subarray(V * D, V * D + V);
    this.pc = arr.subarray(V * D + V);
    (head.vocab as string[]).forEach((w, i) => this.vocab.set(w, i));
    this.dim = D;
    this.a = f(head.a ?? 1e-3);
    this.name = `${head.name ?? "sif"}@${String(head.sha256).slice(0, 12)}`;
  }

  embed(text: string): Float32Array {
    const hit = this.cache.get(text);
    if (hit) return hit;
    const D = this.dim;
    const ids: number[] = [];
    for (const w of words(text)) {
      const i = this.vocab.get(w);
      if (i !== undefined) ids.push(i);
    }
    let r = new Float32Array(D);
    if (ids.length) {
      // (vecs[ids] * wts[:, None]).sum(0) / len(ids): float32, rows accumulated in order. The weighted rows
      // f(vecs[i] * wt_i) depend only on the word, so they are computed once per word and reused.
      for (const i of ids) {
        const w = this.weighted(i);
        for (let j = 0; j < D; j++) r[j] = r[j]! + w[j]!;
      }
      for (let j = 0; j < D; j++) r[j] = r[j]! / ids.length;
      let d = 0;
      for (let j = 0; j < D; j++) d += r[j]! * this.pc[j]!;
      const dp = f(d);
      for (let j = 0; j < D; j++) r[j] = r[j]! - f(this.pc[j]! * dp);
      let ss = 0;
      for (let j = 0; j < D; j++) ss += r[j]! * r[j]!;
      const n = f(Math.sqrt(f(ss)));
      if (n) for (let j = 0; j < D; j++) r[j] = r[j]! / n;
    } else {
      r = new Float32Array(D);
    }
    // a small bounded cache: indexes keep their own copy of every vector, so this only saves recomputation of
    // recently seen texts (queries, segment windows shared by neighbouring units)
    if (this.cache.size >= 20_000) this.cache.delete(this.cache.keys().next().value!);
    this.cache.set(text, r);
    return r;
  }

  forget(content: string): void {
    forgetCached(this.cache, content);
  }
}

function forgetCached(cache: Map<string, Float32Array>, content: string) {
  if (!content) return;
  for (const key of [...cache.keys()]) {
    const i = key.indexOf(": ");
    const body = i >= 0 ? key.slice(i + 2) : key;
    if (key.includes(content) || (body.length > 12 && content.includes(body))) cache.delete(key);
  }
}

/** OpenAI-compatible embeddings API. Vectors are normalised; the key is read from the named environment variable. */
export class ApiEncoder implements Encoder {
  readonly name: string;
  private cache = new Map<string, Float32Array>();
  constructor(readonly url: string, readonly model: string, readonly dim: number, private keyEnv = "SMARTWARE_EMBED_KEY") {
    this.name = `api:${model}`;
  }

  persist: VectorCache | null = null;

  has(text: string): boolean {
    return this.cache.has(text);
  }

  async warm(texts: string[]): Promise<void> {
    let need = [...new Set(texts.filter((t) => !this.cache.has(t)))];
    if (this.cache.size > 200_000) this.cache.clear(); // indexes hold their own copies; keep this bounded
    if (this.persist && need.length) {
      const still: string[] = [];
      for (const t of need) {
        const v = await this.persist.get(this.name, sha(t));
        if (v) this.cache.set(t, v);
        else still.push(t);
      }
      need = still;
    }
    const key = process.env[this.keyEnv];
    for (let i = 0; i < need.length; i += 128) {
      const batch = need.slice(i, i + 128);
      const res = await fetch(this.url, {
        method: "POST",
        headers: { "content-type": "application/json", ...(key ? { authorization: `Bearer ${key}` } : {}) },
        body: JSON.stringify({ model: this.model, input: batch.map((t) => t || " ") }),
      });
      if (!res.ok) throw new Error(`embedding API returned ${res.status}`);
      const body = await res.json() as { data: Array<{ embedding: number[]; index: number }> };
      for (const row of body.data) {
        const v = Float32Array.from(row.embedding);
        let ss = 0;
        for (const x of v) ss += x * x;
        const n = Math.sqrt(ss);
        if (n) for (let j = 0; j < v.length; j++) v[j] = v[j]! / n;
        this.cache.set(batch[row.index]!, v);
        await this.persist?.put(this.name, sha(batch[row.index]!), v);
      }
    }
  }

  embed(text: string): Float32Array {
    const v = this.cache.get(text);
    if (!v) throw new Error("ApiEncoder.embed before warm(): no vector for this text");
    return v;
  }

  forget(content: string): void {
    forgetCached(this.cache, content);
  }

  forgetTexts(texts: string[]): void {
    for (const t of texts) {
      this.cache.delete(t);
      this.persist?.delete?.(this.name, sha(t));
    }
  }
}

export function loadEncoder(spec: string | undefined | null): Encoder {
  if (!spec || spec === "hashing") return new HashingEncoder();
  if (spec.startsWith("sif:")) return new SifEncoder(spec.slice(4));
  if (spec.startsWith("openai:")) {
    // openai:<model>:<dim>[:<url>]
    const [, model, dim, ...url] = spec.split(":");
    return new ApiEncoder(url.length ? url.join(":") : "https://api.openai.com/v1/embeddings", model!, Number(dim));
  }
  throw new Error(`unknown encoder ${JSON.stringify(spec)}`);
}
