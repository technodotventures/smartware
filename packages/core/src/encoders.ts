// Encoders (the engine's Encoder port). ORP does not standardise embeddings (spec §3.1); an engine declares the one
// it uses. Built in:
//   hashing  feature-hashed words and word bigrams (BLAKE2b-64), no weights, no network, deterministic.
//   sif      PPMI-SVD word vectors + SIF sentence embedding, read from a data-only .orpenc file.
//   openai   any OpenAI-compatible /embeddings endpoint; the key comes from the environment, never from a file.
// Recall itself is synchronous, so remote encoders are warmed asynchronously before the engine runs.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { blake2b } from "./blake2b.js";
import { f } from "./f32.js";
import { words } from "./text.js";

export interface Encoder {
  readonly name: string;
  readonly dim: number;
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
  get(encoder: string, sha: string): Float32Array | null;
  put(encoder: string, sha: string, v: Float32Array): void;
  delete?(encoder: string, sha: string): void;
}

const enc = new TextEncoder();

export class HashingEncoder implements Encoder {
  readonly name: string;
  constructor(readonly dim = 512) {
    this.name = `hashing-${dim}`;
  }

  // token -> signed bucket (bucket + 1, negated for -1). Vocabulary-bounded, so it stays small; it removes repeated
  // BLAKE2b work when the index is rebuilt or the same words recur.
  private tokCache = new Map<string, number>();

  private bucket(tok: string): number {
    let b = this.tokCache.get(tok);
    if (b === undefined) {
      const d = blake2b(enc.encode(tok), 8);
      let idx: number;
      if ((this.dim & (this.dim - 1)) === 0 && this.dim <= 1 << 24) {
        // power-of-two dim: h % dim is the low bits of the big-endian integer
        idx = ((d[4]! << 24) | (d[5]! << 16) | (d[6]! << 8) | d[7]!) >>> 0;
        idx = idx & (this.dim - 1);
      } else {
        let h = 0n;
        for (const x of d) h = (h << 8n) | BigInt(x); // int(hexdigest, 16): big-endian
        idx = Number(h % BigInt(this.dim));
      }
      b = (d[0]! & 0x80) ? idx + 1 : -(idx + 1);
      if (this.tokCache.size > 2_000_000) this.tokCache.clear();
      this.tokCache.set(tok, b);
    }
    return b;
  }

  embed(text: string): Float32Array {
    const ws = words(text);
    const v = new Float32Array(this.dim);
    const add = (tok: string) => {
      const b = this.bucket(tok);
      if (b > 0) v[b - 1]! += 1.0; else v[-b - 1]! -= 1.0;
    };
    for (const w of ws) add(w);
    for (let i = 0; i + 1 < ws.length; i++) add(ws[i] + "_" + ws[i + 1]);
    let ss = 0;
    for (const x of v) ss += x * x;
    const n = f(Math.sqrt(f(ss)));
    if (n) for (let i = 0; i < v.length; i++) v[i] = f(v[i]! / n);
    return v;
  }
}

const MAGIC = "ORPENC1\n";
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
    if (data.subarray(0, 8).toString("latin1") !== MAGIC) throw new Error(`${path}: not an .orpenc file`);
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
  constructor(readonly url: string, readonly model: string, readonly dim: number, private keyEnv = "ORP_EMBED_KEY") {
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
      need = need.filter((t) => {
        const v = this.persist!.get(this.name, sha(t));
        if (v) this.cache.set(t, v);
        return !v;
      });
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
        this.persist?.put(this.name, sha(batch[row.index]!), v);
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
