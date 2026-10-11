// ORP product engine: a faithful port of the reference engine (orp/engine.py), restructured for production.
//
// What changed from the oracle, and why recall is unchanged:
//  - One partition per tenant. Every recall quantity is computed over the asker's admissible set, which never
//    crosses a tenant, so partitioning cannot change a result; it only stops one tenant's writes from rebuilding
//    another's index.
//  - Incremental index. Appends add a unit to its partition (BM25 postings, vectors, thread order) instead of
//    rebuilding everything; content changes and removals rebuild only that partition.
//  - Float32 steps are reproduced where the oracle keeps float32 arrays (see f32.ts).
// The oracle stays the specification: the differential harness (tools/parity) compares packs.
import { createHmac, randomBytes } from "node:crypto";
import { BM25, type Mask } from "./bm25.js";
import type { Encoder } from "./encoders.js";
import { dot32, f, mean32, pairwiseSum32 } from "./f32.js";
import { claimKey, dedupKey, idemDedupKey, type ContextItem, type ContextPack, type Memory, Principal,
  type RecallRequest, SYSTEM } from "./model.js";
import { FEATURES, type GbdtModel, type Params, defaultParams } from "./params.js";
import { countTokens, counter, sentences, STOPWORDS, terms } from "./text.js";
import { epochSeconds, isoDate, isoformat, MIN_TIME, nowMicros, parseTime, strftime, type Micros } from "./time.js";
import { parseQuery, resolveRelative, type TimeIntent } from "./timeparse.js";

export const SENSITIVE = ["secret", "pii", "health", "legal"];
const TIME_RX = /\b(yesterday|today|tomorrow|tonight|last|next|ago|week|weekend|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|september|october|november|december|\d{4}|\d{1,2}(st|nd|rd|th))\b/i;
const NUM_RX = /\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|twice|once|several|dozen)\b/i;
const CAP_RX = /\b[A-Z][a-zA-Z'\-]+\b/g;
const ENT_RX = /(?<![.!?]\s)(?<!^)\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b/g;
const SPLIT_RX = /\bbetween\b|\band\b|\bor\b|\bversus\b|\bvs\.?\b|,|;|\bafter\b|\bbefore\b|\bthan\b|\bsince\b|\bwhen\b|\bwhile\b/;

export function entities(text: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(ENT_RX)) {
    const e = m[1]!;
    if (STOPWORDS.has(e.toLowerCase()) || e.length < 3) continue;
    if (!out.includes(e)) out.push(e);
  }
  return out.slice(0, 12);
}

function splitQuery(q: string): string[] {
  const parts = q.split(SPLIT_RX).map((x) => x.trim()).filter(Boolean);
  const good = parts.filter((x) => terms(x).length >= 2);
  return good.length >= 2 ? good : [];
}

const pyRstrip = (s: string) => s.replace(/\s+$/u, "");
const pyRound = (x: number, n: number) => Number(x.toFixed(n));
const lowerNoS = (w: string) => { const l = w.toLowerCase(); return l.endsWith("'s") ? l.slice(0, -2) : l; };

/** Admissible prev/next and everything derived per unit, for one tenant. */
class Partition {
  ids: string[] = [];
  pos = new Map<string, number>();
  mems: Memory[] = [];
  unitTxt: string[] = [];
  bm = new BM25();
  bmSeg = new BM25();
  segUnit: number[] = [];
  E: Float32Array;
  ES: Float32Array;
  threadIndex = new Map<string, number>();
  threadUnits: number[][] = [];
  threadNamed: boolean[] = [];
  threadOf: number[] = [];
  ent = new Map<string, number[]>();
  ts: number[] = [];
  ntok: number[] = [];
  isAssistant: boolean[] = [];
  unitTerms: Array<Set<string>> = [];
  hasTime: boolean[] = [];
  hasNum: boolean[] = [];
  hasPlace: boolean[] = [];
  isQ: boolean[] = [];
  actorL: string[] = [];

  constructor(readonly tenant: string, readonly dim: number) {
    this.E = new Float32Array(dim * 64);
    this.ES = new Float32Array(dim * 64);
  }

  get n(): number {
    return this.ids.length;
  }

  private grow(a: Float32Array, rows: number): Float32Array {
    if (rows * this.dim <= a.length) return a;
    const b = new Float32Array(Math.max(rows * this.dim, a.length * 2));
    b.set(a);
    return b;
  }

  add(m: Memory, enc: Encoder, segSentences: number) {
    const k = this.ids.length;
    this.ids.push(m.id);
    this.pos.set(m.id, k);
    this.mems.push(m);
    const extra = ((m.meta.time_refs ?? []) as Array<[string, string]>).map((x) => x[1]).join(" ");
    const ut = `${m.actor}: ${m.content} ${extra}`.trim();
    this.unitTxt.push(ut);
    this.bm.add(ut);
    this.E = this.grow(this.E, k + 1);
    this.E.set(enc.embed(`${m.actor}: ${m.content}`), k * this.dim);
    const ss = sentences(m.content);
    const segs: string[] = [];
    if (ss.length <= segSentences) segs.push(ut);
    else for (let i = 0; i + segSentences <= ss.length; i++) segs.push(`${m.actor}: ` + ss.slice(i, i + segSentences).join(" "));
    for (const s of segs) {
      const j = this.segUnit.length;
      this.segUnit.push(k);
      this.bmSeg.add(s);
      this.ES = this.grow(this.ES, j + 1);
      this.ES.set(enc.embed(s), j * this.dim);
    }
    // thread order: (seq, observed_at), stable, so a new unit goes after every unit with an equal key
    let ti = this.threadIndex.get(m.thread);
    if (ti === undefined) {
      ti = this.threadUnits.length;
      this.threadIndex.set(m.thread, ti);
      this.threadUnits.push([]);
      this.threadNamed.push(Boolean(m.thread));
    }
    this.threadOf.push(ti);
    const tu = this.threadUnits[ti]!;
    const key = (x: Memory) => [x.seq, x.observed_at ?? MIN_TIME] as const;
    const [s0, o0] = key(m);
    let lo = 0, hi = tu.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const [s1, o1] = key(this.mems[tu[mid]!]!);
      if (s1 < s0 || (s1 === s0 && o1 <= o0)) lo = mid + 1; else hi = mid;
    }
    tu.splice(lo, 0, k);
    for (const e of (m.meta.entities ?? []) as string[]) this.entAdd(e.toLowerCase(), k);
    if (m.actor) this.entAdd(m.actor.toLowerCase(), k);
    this.ts.push(epochSeconds(m.event_time ?? m.observed_at ?? 0));
    this.ntok.push(countTokens(ut));
    this.isAssistant.push(m.role === "assistant");
    this.unitTerms.push(new Set(terms(ut)));
    this.hasTime.push(Boolean((m.meta.time_refs ?? []).length) || TIME_RX.test(m.content));
    this.hasNum.push(NUM_RX.test(m.content));
    this.hasPlace.push(Boolean((m.meta.entities ?? []).length));
    this.isQ.push(pyRstrip(m.content).endsWith("?"));
    this.actorL.push(m.actor.toLowerCase());
  }

  private entAdd(e: string, k: number) {
    let a = this.ent.get(e);
    if (!a) this.ent.set(e, (a = []));
    if (a[a.length - 1] !== k) a.push(k);
  }
}

export interface Channel { w: number; ranks: Map<number, number> }

export interface Journal {
  mem: Set<string>; // memories written or changed
  deleted: Set<string>; // memories purged
  dedupAdd: Map<string, string>;
  dedupDel: Set<string>;
  claims: Set<string>; // claim chain keys (tenant \0 key) changed
  tenants: Set<string>; // counters changed
}

export function newJournal(): Journal {
  return { mem: new Set(), deleted: new Set(), dedupAdd: new Map(), dedupDel: new Set(), claims: new Set(),
    tenants: new Set() };
}

export class Engine {
  p: Params;
  readonly mem = new Map<string, Memory>();
  /** per-tenant insertion order */
  readonly order = new Map<string, string[]>();
  readonly ord = new Map<string, number>(); // global insertion sequence (persisted)
  ordSeq = 0;
  readonly hash = new Map<string, string>();
  private hashOf = new Map<string, string[]>();
  readonly claims = new Map<string, string[]>();
  audit: Array<Record<string, any>> = [];
  mutations = 0;
  readonly tmut = new Map<string, number>();
  readonly tcount = new Map<string, number>();
  epoch = 0;
  wseq = 0;
  secret: Buffer = randomBytes(16);
  hasExpiry = false;
  viewSup = new Map<string, string>();
  journal: Journal = newJournal();
  private parts = new Map<string, Partition>();
  private dirtyParts = new Set<string>();
  // recall scratch shared with featurize/pack
  private P!: Partition;
  private pm!: Int32Array;
  private nm!: Int32Array;
  private mult = new Map<number, Map<string, number>>();
  private hasRolesQ = false;
  private asksAssistant = false;
  private nParts = 0;
  private qtext = "";
  collect = false;
  last: [number[], Float32Array[]] | null = null;

  constructor(readonly enc: Encoder, params?: Params) {
    this.p = params ?? defaultParams();
  }

  // ------------------------------------------------------------ write
  touch(tenant: string | null) {
    this.mutations++;
    if (tenant === null) {
      this.epoch++;
      for (const t of this.parts.keys()) this.dirtyParts.add(t);
    } else {
      this.tmut.set(tenant, (this.tmut.get(tenant) ?? 0) + 1);
      this.journal.tenants.add(tenant);
    }
  }

  /** Mark a partition for rebuild (content changed or a unit was removed). */
  invalidate(tenant: string) {
    this.dirtyParts.add(tenant);
  }

  private hashSet(h: string, id: string) {
    this.hash.set(h, id);
    let a = this.hashOf.get(id);
    if (!a) this.hashOf.set(id, (a = []));
    a.push(h);
    this.journal.dedupAdd.set(h, id);
    this.journal.dedupDel.delete(h);
  }

  dropHashes(id: string) {
    for (const h of this.hashOf.get(id) ?? []) {
      if (this.hash.get(h) === id) {
        this.hash.delete(h);
        this.journal.dedupDel.add(h);
        this.journal.dedupAdd.delete(h);
      }
    }
    this.hashOf.delete(id);
  }

  /** Restore a de-duplication entry from the store. */
  loadHash(h: string, id: string) {
    this.hash.set(h, id);
    let a = this.hashOf.get(id);
    if (!a) this.hashOf.set(id, (a = []));
    a.push(h);
  }

  dirty(id: string) {
    this.journal.mem.add(id);
  }

  appendOrder(m: Memory) {
    let o = this.order.get(m.tenant);
    if (!o) this.order.set(m.tenant, (o = []));
    o.push(m.id);
    this.ord.set(m.id, ++this.ordSeq);
  }

  removeOrder(id: string, tenant: string) {
    const o = this.order.get(tenant);
    if (o) {
      const i = o.indexOf(id);
      if (i >= 0) o.splice(i, 1);
    }
    this.ord.delete(id);
    this.invalidate(tenant);
  }

  remember(items: Memory | Memory[], principal: Principal = SYSTEM, chainOnly = false): string[] {
    const list = Array.isArray(items) ? items : [items];
    const ids: string[] = [];
    const touched = new Set<string>();
    for (const m of list) {
      if (m.meta._creator === undefined) m.meta._creator = principal.id;
      const writer = m.meta._creator as string;
      if (m.derived_from.length) {
        const [acl, labels] = this.closure(m);
        if (acl !== null) m.readers = m.readers === null ? new Set(acl) : new Set([...m.readers].filter((x) => acl.has(x)));
        m.labels = [...new Set([...m.labels, ...labels])].sort();
      }
      const ik = m.meta.idempotency_key as string | undefined;
      const h = ik ? idemDedupKey(m.tenant, writer, ik) : dedupKey(m, writer);
      const prev = this.hash.get(h);
      if (prev !== undefined && this.mem.has(prev)) {
        ids.push(prev);
        continue;
      }
      if (m.observed_at === null) m.observed_at = nowMicros();
      if (m.valid_from === null) m.valid_from = m.event_time ?? m.observed_at;
      if (!("_valid_to" in m.meta)) m.meta._valid_to = m.valid_to !== null ? isoformat(m.valid_to) : null;
      this.wseq++;
      if (!("_wseq" in m.meta)) m.meta._wseq = this.wseq;
      if (this.p.resolve_dates && m.observed_at !== null && (m.kind === "event" || m.kind === "document")) {
        const refs = resolveRelative(m.content, m.event_time ?? m.observed_at);
        if (refs.length) m.meta.time_refs = refs;
      }
      if (!("entities" in m.meta)) m.meta.entities = entities(m.content);
      this.mem.set(m.id, m);
      this.appendOrder(m);
      this.tcount.set(m.tenant, (this.tcount.get(m.tenant) ?? 0) + 1);
      this.hasExpiry ||= m.expires_at !== null;
      this.hashSet(h, m.id);
      if (ik && !this.hash.has(dedupKey(m, writer))) this.hashSet(dedupKey(m, writer), m.id);
      this.dirty(m.id);
      const part = this.parts.get(m.tenant);
      if (part && !this.dirtyParts.has(m.tenant)) part.add(m, this.enc, this.p.seg_sentences);
      if (m.claim !== null && m.kind !== "event" && m.kind !== "document") {
        if (chainOnly) {
          const ck = m.tenant + "\0" + claimKey(m.claim);
          let ch = this.claims.get(ck);
          if (!ch) this.claims.set(ck, (ch = []));
          if (!ch.includes(m.id)) ch.push(m.id);
          this.journal.claims.add(ck);
        } else {
          this.supersede(m, principal);
        }
      }
      this.audit.push({ op: "remember", id: m.id, by: principal.id, at: isoformat(nowMicros()) });
      ids.push(m.id);
      touched.add(m.tenant);
    }
    for (const t of touched) this.touch(t);
    return ids;
  }

  closure(d: Memory): [Set<string> | null, Set<string>] {
    let acl: Set<string> | null = null;
    const labels = new Set<string>();
    for (const s of d.derived_from) {
      const sm = this.mem.get(s);
      if (!sm) continue;
      const rd = sm.readers;
      if (rd !== null) acl = acl === null ? new Set<string>(rd) : new Set<string>([...acl].filter((x: string) => rd.has(x)));
      for (const l of sm.labels) if (SENSITIVE.includes(l)) labels.add(l);
    }
    return [acl, labels];
  }

  // ---- supersession (§5.6)
  supOrder(i: string): [number, number, number] {
    const m = this.mem.get(i)!;
    return [m.valid_from ?? MIN_TIME, m.observed_at ?? MIN_TIME, (m.meta._wseq as number) ?? 0];
  }

  private supCmp = (a: string, b: string) => {
    const x = this.supOrder(a), y = this.supOrder(b);
    return x[0] - y[0] || x[1] - y[1] || x[2] - y[2];
  };

  static covers(nw: ReadonlySet<string> | null, old: ReadonlySet<string> | null): boolean {
    if (nw === null) return true;
    if (old === null) return false;
    for (const x of old) if (!nw.has(x)) return false;
    return true;
  }

  static inChain(m: Memory): boolean {
    return m.claim !== null && m.claim.cardinality === "one" && m.kind !== "event" && m.kind !== "document" &&
      !m.meta._explicit_superseded;
  }

  authoredValidTo(m: Memory): Micros | null {
    const v = m.meta._valid_to;
    return typeof v === "string" ? parseTime(v) : (typeof v === "number" ? v : null);
  }

  supersede(m: Memory, principal: Principal = SYSTEM) {
    const key = m.tenant + "\0" + claimKey(m.claim!);
    let chain = this.claims.get(key);
    if (!chain) this.claims.set(key, (chain = []));
    if (!chain.includes(m.id)) chain.push(m.id);
    this.journal.claims.add(key);
    if (m.claim!.cardinality !== "one") return;
    for (const oid of chain) {
      const o = this.mem.get(oid);
      if (oid === m.id || !o || !Engine.inChain(o)) continue;
      const [old, nw] = this.supCmp(oid, m.id) < 0 ? [o, m] : [m, o];
      if (old === o && !this.canWrite(o, principal)) continue;
      const lst: string[] = (old.meta._supersedable_by ??= []);
      if (!lst.includes(nw.id)) {
        lst.push(nw.id);
        this.dirty(old.id);
      }
    }
    this.resolveChain(key);
  }

  resolveChain(key: string) {
    const chain = (this.claims.get(key) ?? []).filter((i) => this.mem.has(i)).sort(this.supCmp);
    const live = chain.filter((i) => { const x = this.mem.get(i)!; return (x.status === "active" || x.status === "superseded") && Engine.inChain(x); });
    live.forEach((i, r) => {
      const x = this.mem.get(i)!;
      const ok = new Set<string>(x.meta._supersedable_by ?? []);
      const obj = x.claim!.object.trim().toLowerCase();
      const succ = live.slice(r + 1).find((j) => ok.has(j) && this.mem.get(j)!.claim!.object.trim().toLowerCase() !== obj &&
        Engine.covers(this.mem.get(j)!.readers, x.readers));
      if (succ !== undefined) {
        if (x.status !== "superseded" || x.superseded_by !== succ) {
          x.status = "superseded"; x.superseded_by = succ; x.valid_to = this.mem.get(succ)!.valid_from;
          x.version++;
          this.dirty(i);
        }
      } else if (x.status !== "active" || x.superseded_by !== null) {
        x.status = "active"; x.superseded_by = null; x.valid_to = this.authoredValidTo(x);
        x.version++;
        this.dirty(i);
      }
    });
    const inChainSet = new Set(chain);
    for (const i of live) {
      const y = this.mem.get(i)!;
      const before = y.supersedes.join("\0");
      y.supersedes = y.supersedes.filter((j) => !inChainSet.has(j) || this.mem.get(j)?.superseded_by === i);
      for (const j of live) if (this.mem.get(j)!.superseded_by === i && !y.supersedes.includes(j)) y.supersedes.push(j);
      if (y.supersedes.join("\0") !== before) this.dirty(i);
    }
  }

  /** Re-apply provenance-closed ACL and sensitivity labels to everything derived (transitively) from memId. */
  reclose(memId: string): string[] {
    const changed: string[] = [];
    const frontier = [memId];
    const seen = new Set<string>();
    while (frontier.length) {
      const cur = frontier.pop()!;
      for (const d of this.mem.values()) {
        if (!d.derived_from.includes(cur) || seen.has(d.id)) continue;
        seen.add(d.id);
        const [acl, labels] = this.closure(d);
        const nw = acl === null ? d.readers : new Set(d.readers === null ? acl : [...d.readers].filter((x) => acl.has(x)));
        const newLabels = [...new Set([...d.labels, ...labels])].sort();
        const same = setEq(nw, d.readers) && newLabels.join("\0") === [...d.labels].sort().join("\0");
        if (!same) {
          d.readers = nw; d.labels = newLabels; d.version++;
          this.dirty(d.id);
          changed.push(d.id);
        }
        frontier.push(d.id);
      }
    }
    const m = this.mem.get(memId);
    this.touch(m ? m.tenant : null);
    return changed;
  }

  forget(memId: string, mode = "retract", cascade = true, principal: Principal = SYSTEM, reason = ""): string[] {
    const hit = this.cascadeSet([memId], cascade);
    const tenant = this.mem.get(memId)?.tenant ?? null;
    for (const i of hit) {
      const m = this.mem.get(i);
      if (!m) continue;
      if (mode === "purge") {
        this.mem.delete(i);
        this.removeOrder(i, m.tenant);
        this.tcount.set(m.tenant, (this.tcount.get(m.tenant) ?? 0) - 1);
        this.dropHashes(i);
        this.enc.forget?.(m.content);
        this.journal.deleted.add(i);
        this.journal.mem.delete(i);
      } else {
        m.status = "retracted";
        m.version++;
        if (mode === "redact") {
          this.enc.forget?.(m.content);
          m.content = "[redacted]";
          delete m.meta.time_refs;
          delete m.meta.entities;
          if (m.claim) m.claim.subject = m.claim.predicate = m.claim.object = "[redacted]";
          for (const s of m.sources) { s.span = ""; s.hash = ""; }
          this.invalidate(m.tenant); // the index must not keep redacted text
        }
        this.dirty(i);
      }
      this.audit.push({ op: `forget:${mode}`, id: i, by: principal.id, reason, at: isoformat(nowMicros()) });
    }
    const keys = new Set<string>();
    for (const i of hit) {
      const m = this.mem.get(i);
      if (m?.claim) keys.add(m.tenant + "\0" + claimKey(m.claim));
    }
    for (const k of keys) this.resolveChain(k); // a retracted superseder restores the version it replaced
    this.touch(tenant);
    return hit;
  }

  cascadeSet(ids: string[], cascade = true): string[] {
    const hit = [...new Set(ids)];
    let changed = true;
    while (changed) {
      changed = false;
      const hs = new Set(hit);
      for (const m of this.mem.values()) {
        if (!m.derived_from.length || hs.has(m.id)) continue;
        const gone = m.derived_from.map((s) => hs.has(s) || !this.mem.has(s) || this.mem.get(s)!.status === "retracted");
        if ((cascade && m.derived_from.some((s) => hs.has(s))) || gone.every(Boolean)) {
          hit.push(m.id);
          hs.add(m.id);
          changed = true;
        }
      }
    }
    return hit;
  }

  history(subject: string, predicate: string, tenant = "default"): Memory[] {
    const key = tenant + "\0" + `${subject.toLowerCase().trim()}|${predicate.toLowerCase().trim()}`;
    return (this.claims.get(key) ?? []).filter((i) => this.mem.has(i)).map((i) => this.mem.get(i)!);
  }

  // ------------------------------------------------------------ index
  partition(tenant: string): Partition {
    let part = this.parts.get(tenant);
    if (!part || this.dirtyParts.has(tenant)) {
      part = new Partition(tenant, this.enc.dim);
      for (const i of this.order.get(tenant) ?? []) {
        const m = this.mem.get(i);
        if (m) part.add(m, this.enc, this.p.seg_sentences);
      }
      this.parts.set(tenant, part);
      this.dirtyParts.delete(tenant);
    }
    return part;
  }

  /** Texts the encoder must embed before the given tenant's index is (re)built (for remote encoders). */
  pendingTexts(tenant: string, extra: Memory[] = []): string[] {
    const out: string[] = [];
    const add = (m: Memory) => {
      out.push(`${m.actor}: ${m.content}`);
      const ss = sentences(m.content);
      const extraT = ((m.meta.time_refs ?? []) as Array<[string, string]>).map((x) => x[1]).join(" ");
      if (ss.length <= this.p.seg_sentences) out.push(`${m.actor}: ${m.content} ${extraT}`.trim());
      else for (let i = 0; i + this.p.seg_sentences <= ss.length; i++) out.push(`${m.actor}: ` + ss.slice(i, i + this.p.seg_sentences).join(" "));
    };
    if (!this.parts.has(tenant) || this.dirtyParts.has(tenant)) for (const i of this.order.get(tenant) ?? []) add(this.mem.get(i)!);
    for (const m of extra) add(m);
    return out;
  }

  // ------------------------------------------------------------ access
  readable(m: Memory, pr: Principal, labels: readonly string[] = []): boolean {
    if (m.tenant !== pr.tenant) return false;
    const star = pr.isStar;
    if (!star && m.readers !== null) {
      const tok = pr.tokens();
      let ok = false;
      for (const r of m.readers) if (tok.has(r)) { ok = true; break; }
      if (!ok) return false;
    }
    if (m.labels.includes("secret") && !(labels.includes("secret") && (star || pr.tokens().has("label:secret")))) return false;
    return true;
  }

  canWrite(m: Memory, pr: Principal): boolean {
    if (m.tenant !== pr.tenant) return false;
    if (pr.isStar) return true;
    if (m.writers === null) return m.meta._creator === pr.id;
    const tok = pr.tokens();
    for (const w of m.writers) if (tok.has(w)) return true;
    return false;
  }

  static validAt(m: Memory, t: Micros): boolean {
    if (m.valid_from !== null && m.valid_from > t) return false;
    if (m.valid_to !== null && t >= m.valid_to) return false;
    return true;
  }

  private maskFor(P: Partition, req: RecallRequest): Mask {
    const asOf = req.as_of ?? nowMicros();
    const now = nowMicros(); // retention is real time, not the as_of view (§7)
    const labels = req.filters.labels ?? [];
    const drafts = Boolean(req.filters.drafts);
    const sc = req.scope.replace(/\/+$/, "");
    const ok = new Uint8Array(P.n);
    const chainKs: number[] = [];
    for (let k = 0; k < P.n; k++) {
      const m = P.mems[k]!;
      if (!this.readable(m, req.principal, labels)) continue;
      if (m.status === "retracted" || m.status === "expired") continue;
      if (m.status === "draft" && !(drafts && this.canWrite(m, req.principal))) continue;
      if (req.known_at !== null && m.observed_at !== null && m.observed_at > req.known_at) continue;
      if (m.expires_at !== null && m.expires_at <= now) continue;
      const chain = Engine.inChain(m);
      if (m.valid_from !== null && m.valid_from > asOf) continue;
      const vt = chain ? this.authoredValidTo(m) : m.valid_to;
      if (vt !== null && asOf >= vt && !req.include_history) continue;
      if (sc && !(m.scope === sc || m.scope.startsWith(sc + "/"))) continue;
      if (req.kinds && !req.kinds.includes(m.kind)) continue;
      ok[k] = 1;
      if (chain) chainKs.push(k);
    }
    // claim view: an admissible version is current unless a newer admissible version that may supersede it states a
    // different object
    this.viewSup = new Map();
    const by = new Map<string, string[]>();
    for (const k of chainKs) {
      const m = P.mems[k]!;
      const key = m.tenant + "\0" + claimKey(m.claim!);
      let a = by.get(key);
      if (!a) by.set(key, (a = []));
      a.push(m.id);
    }
    for (const ids of by.values()) {
      if (ids.length < 2) continue;
      ids.sort(this.supCmp);
      ids.forEach((i, r) => {
        const x = this.mem.get(i)!;
        const okset = new Set<string>(x.meta._supersedable_by ?? []);
        const obj = x.claim!.object.trim().toLowerCase();
        const succ = ids.slice(r + 1).find((j) => okset.has(j) && this.mem.get(j)!.claim!.object.trim().toLowerCase() !== obj);
        if (succ !== undefined) {
          this.viewSup.set(i, succ);
          if (!req.include_history) ok[P.pos.get(i)!] = 0;
        }
      });
    }
    return ok;
  }

  /** Ids admissible for the request (the same set every recall channel draws from). */
  admissible(req: RecallRequest): string[] {
    const P = this.partition(req.principal.tenant);
    if (!P.n) return [];
    const mask = this.maskFor(P, req);
    const out: string[] = [];
    for (let k = 0; k < P.n; k++) if (mask[k]) out.push(P.ids[k]!);
    return out;
  }

  // ------------------------------------------------------------ recall
  recall(req: RecallRequest): ContextPack {
    if (!this.p.admissible_stats) throw new Error("admissible_stats=false is an oracle-only experiment");
    const P = this.partition(req.principal.tenant);
    this.P = P;
    const n = P.n;
    const tenant = req.principal.tenant;
    if (this.mem.size === 0) return { items: [], tokens: 0, budget: req.budget_tokens, coverage: 0, snapshot: this.snapshot(tenant), notes: [] };
    const p = this.p;
    const now = req.as_of ?? nowMicros();
    const mask = this.maskFor(P, req);
    const adm: number[] = [];
    for (let k = 0; k < n; k++) if (mask[k]) adm.push(k);
    if (!adm.length) { // an empty tenant in a non-empty store, as the oracle reports it
      return { items: [], tokens: 0, budget: req.budget_tokens, coverage: 0, snapshot: this.snapshot(tenant),
        notes: ["no admissible memories"] };
    }
    const notes: string[] = [];
    const q = req.query;
    this.qtext = q;
    const ti = parseQuery(q, now);
    const qterms = [...new Set(terms(q))];
    const qset = new Set(qterms);
    const D = this.enc.dim;
    let qvec: Float32Array;
    try {
      qvec = this.enc.embed(q);
    } catch {
      qvec = new Float32Array(D); // spec §5.8: report, never fail silently
    }
    if (!qvec.some((x) => x !== 0)) notes.push("degraded:dense_unavailable");

    const { pm, nm } = this.admissibleNeighbours(P, mask);
    this.pm = pm; this.nm = nm;

    const topranks = (score: ArrayLike<number>, depth = p.depth): Map<number, number> => {
      let mx = -Infinity;
      for (const k of adm) if (score[k]! > mx) mx = score[k]!;
      const cands = mx > 0 ? adm.filter((k) => score[k]! > 0) : adm;
      const order = topK(cands, score, depth);
      const out = new Map<number, number>();
      order.forEach((k, r) => out.set(k, r));
      return out;
    };
    const nb = (arr: ArrayLike<number>, ix: Int32Array, k: number) => (ix[k]! >= 0 ? arr[ix[k]!]! : 0);

    const chans = new Map<string, Channel>();
    const sBm = P.bm.score(q, mask);
    chans.set("bm25", { w: p.w_bm25, ranks: topranks(sBm) });
    const sWin = new Float32Array(n);
    for (const k of adm) sWin[k] = f(sBm[k]! + f(0.5 * f(nb(sBm, pm, k) + nb(sBm, nm, k))));
    chans.set("bm25_win", { w: p.w_bm25_win, ranks: topranks(sWin) });
    const sDense = new Float32Array(n);
    for (const k of adm) sDense[k] = dot32(P.E, k * D, qvec, 0, D);
    const tmp = new Float32Array(n);
    for (const k of adm) tmp[k] = f(sDense[k]! + 1.0);
    chans.set("dense", { w: p.w_dense, ranks: topranks(tmp) });
    const prod = new Float32Array(D);
    const rowDot = (a: number, b: number) => {
      for (let j = 0; j < D; j++) prod[j] = P.E[a * D + j]! * P.E[b * D + j]!;
      return pairwiseSum32(prod, 0, D);
    };
    const sWinD = new Float64Array(n);
    for (const k of adm) {
      const hp = pm[k]! >= 0 ? 1 : 0, hn = nm[k]! >= 0 ? 1 : 0;
      const dkp = hp ? rowDot(k, pm[k]!) : 0;
      const dkn = hn ? rowDot(k, nm[k]!) : 0;
      const dpn = hp && hn ? rowDot(pm[k]!, nm[k]!) : 0;
      const wnorm = Math.sqrt(Math.max(1e-9, 1 + 0.25 * hp + 0.25 * hn + dkp + dkn + 0.5 * dpn));
      const t1 = f(sDense[k]! + f(0.5 * nb(sDense, pm, k)));
      const t2 = f(t1 + f(0.5 * nb(sDense, nm, k)));
      sWinD[k] = t2 / wnorm + 1.0;
    }
    chans.set("dense_win", { w: p.w_dense_win, ranks: topranks(sWinD) });
    // segments, max-pooled to units
    const segmask = new Uint8Array(P.segUnit.length);
    for (let j = 0; j < P.segUnit.length; j++) segmask[j] = mask[P.segUnit[j]!]!;
    const segScore = P.bmSeg.score(q, segmask);
    const ub = new Float64Array(n);
    const ud = new Float64Array(n).fill(-1);
    for (let j = 0; j < P.segUnit.length; j++) {
      const k = P.segUnit[j]!;
      if (!mask[k]) continue;
      const b = segmask[j] ? segScore[j]! : 0;
      if (b > ub[k]!) ub[k] = b;
      const d = dot32(P.ES, j * D, qvec, 0, D);
      if (d > ud[k]!) ud[k] = d;
    }
    chans.set("bm25_seg", { w: p.w_bm25_seg, ranks: topranks(ub) });
    const ud1 = new Float64Array(n);
    for (const k of adm) ud1[k] = ud[k]! + 1.0;
    chans.set("dense_seg", { w: p.w_dense_seg, ranks: topranks(ud1) });
    // threads
    const T = P.threadUnits.length;
    const thB = new Float64Array(T), thD = new Float64Array(T).fill(-1), first = new Array<number>(T).fill(n);
    const lt: number[] = [];
    for (let t = 0; t < T; t++) {
      const ks = P.threadUnits[t]!.filter((k) => mask[k]);
      if (!ks.length) continue;
      lt.push(t);
      const vals = ks.map((k) => sBm[k]!).sort((a, b) => a - b).slice(-3);
      let s = 0;
      for (const v of vals) s = f(s + v);
      thB[t] = s;
      thD[t] = mean32(ks.map((k) => sDense[k]!));
      first[t] = Math.min(...ks);
    }
    const thRank = new Float64Array(T);
    const rb = [...lt].sort((a, b) => thB[b]! - thB[a]! || first[a]! - first[b]!);
    const rd = [...lt].sort((a, b) => thD[b]! - thD[a]! || first[a]! - first[b]!);
    rb.forEach((t, r) => { thRank[t] = thRank[t]! + 1.0 / (10 + r + 1); });
    rd.forEach((t, r) => { thRank[t] = thRank[t]! + 1.0 / (10 + r + 1); });
    const thScore = new Float64Array(n);
    for (const k of adm) thScore[k] = thRank[P.threadOf[k]!]! + 1e-6;
    chans.set("thread", { w: p.w_thread, ranks: topranks(thScore) });
    // entities named in the query
    const qents = new Set<string>();
    for (const m of q.matchAll(CAP_RX)) {
      const wl = lowerNoS(m[0]);
      const ks = P.ent.get(wl);
      if (ks && !STOPWORDS.has(wl) && ks.some((k) => mask[k])) qents.add(wl);
    }
    if (qents.size) {
      const es = new Float64Array(n);
      for (const e of qents) {
        const ks = P.ent.get(e)!.filter((k) => mask[k]);
        for (const k of ks) es[k] = es[k]! + 1.0 / Math.log(2 + ks.length);
      }
      let mx = 0;
      for (const k of adm) if (es[k]! > mx) mx = es[k]!;
      if (mx > 0) {
        const sc = new Float64Array(n);
        for (const k of adm) sc[k] = es[k]! * (1 + 0.01 * (sBm[k]! > 0 ? 1 : 0)) + 1e-9 * sBm[k]!;
        chans.set("entity", { w: p.w_entity, ranks: topranks(sc) });
      }
    }
    if (ti.window) {
      const lo = epochSeconds(ti.window[0]), hi = epochSeconds(ti.window[1]);
      const inwin = (k: number) => P.ts[k]! >= lo && P.ts[k]! <= hi;
      if (adm.some(inwin)) {
        const sc = new Float64Array(n);
        const C01 = f(0.1);
        for (const k of adm) sc[k] = inwin(k) ? f(sBm[k]! + f(C01 * f(sDense[k]! + 1))) + 1e-6 : 0;
        chans.set("time", { w: p.w_time, ranks: topranks(sc) });
      }
    }
    const qparts = splitQuery(q);
    this.nParts = qparts.length;
    if (qparts.length >= 2 && p.w_sub > 0) {
      const sub = new Float64Array(n);
      for (const pt of qparts) {
        for (const [k, r] of topranks(P.bm.score(pt, mask), 60)) sub[k] = Math.max(sub[k]!, 1.0 / (10 + r));
      }
      chans.set("sub", { w: p.w_sub, ranks: topranks(sub) });
    }
    const fused0 = this.fuse(chans, n);
    if (p.w_prf > 0) {
      const tf = new Map<string, number>();
      for (const k of topK(adm, fused0, p.prf_docs)) {
        if (fused0[k]! <= 0 || !mask[k]) continue;
        const c = counter(terms(P.unitTxt[k]!));
        let tot = 0;
        for (const v of c.values()) tot += v;
        tot = tot || 1;
        for (const [t, v] of c) tf.set(t, (tf.get(t) ?? 0) + v / tot);
      }
      const nadm = Math.max(1, adm.length);
      const common = [...tf.entries()].map((e, i) => [e, i] as const)
        .sort((a, b) => b[0][1] - a[0][1] || a[1] - b[1]).slice(0, p.prf_terms * 3).map((x) => x[0]);
      let exp = common.filter(([t]) => !qset.has(t) && P.bm.vocab.has(t));
      const idf = (t: string) => { const d = P.bm.df(t, mask); return Math.log(1 + (nadm - d + 0.5) / (d + 0.5)); };
      exp = exp.map((e, i) => [e, -e[1] * idf(e[0]), i] as const).sort((a, b) => a[1] - b[1] || a[2] - b[2])
        .slice(0, p.prf_terms).map((x) => x[0]);
      if (exp.length) {
        const mx = Math.max(...exp.map((e) => e[1]));
        const wq = new Map<string, number>();
        for (const t of qterms) wq.set(t, 1.0);
        for (const [t, v] of exp) wq.set(t, (wq.get(t) ?? 0) + p.prf_mix * v / mx);
        chans.set("prf", { w: p.w_prf, ranks: topranks(P.bm.score("", mask, wq)) });
      }
    }
    const fused = this.fuse(chans, n);
    const cand = topK(adm, fused, Math.max(req.max_items * 4, p.pool)).filter((k) => fused[k]! > 0);
    const ql = q.toLowerCase();
    const firstPerson = /\b(i|my|me|mine|i've|i'm|i'd)\b/.test(ql);
    this.hasRolesQ = adm.some((k) => P.isAssistant[k]);
    const asksAssistant = /\b(you|your)\b/.test(ql) && this.hasRolesQ;
    this.asksAssistant = asksAssistant;
    let final = new Map<number, number>();
    this.mult = new Map();
    const topFused = cand.length ? fused[cand[0]!]! : 1.0;
    const qn = Math.max(1, qset.size);
    for (const k of cand) {
      let sc = fused[k]! / topFused;
      const mul = new Map<string, number>();
      let hits = 0;
      const ctxPrev = pm[k]! >= 0 ? P.unitTerms[pm[k]!]! : null;
      for (const t of qset) if (P.unitTerms[k]!.has(t) || (ctxPrev && ctxPrev.has(t))) hits++;
      mul.set("coverage", 1 + p.f_cover * hits / qn);
      if (qents.size && qents.has(P.actorL[k]!)) mul.set("actor", 1 + p.f_actor);
      if (this.hasRolesQ) {
        const role = P.mems[k]!.role;
        if ((asksAssistant && role === "assistant") || (firstPerson && !asksAssistant && role === "user")) mul.set("role", 1 + p.f_role);
      }
      for (const v of mul.values()) sc *= v;
      final.set(k, sc);
      this.mult.set(k, mul);
    }
    let feats: Float32Array[] | null = null;
    if (p.ranker === "linear" || p.ranker === "gbdt" || this.collect) {
      feats = this.featurize(cand, chans, fused, topFused, qset, qents, ti, firstPerson, asksAssistant, sBm, sDense, ub, ud);
      if (this.collect) this.last = [cand, feats];
      if (p.ranker === "linear" && p.lin_w.length && cand.length) {
        const nf = new Map<number, number>();
        cand.forEach((k, i) => {
          const x = feats![i]!;
          let z = 0;
          for (let j = 0; j < x.length; j++) z += ((x[j]! - p.lin_mu[j]!) / p.lin_sd[j]!) * p.lin_w[j]!;
          z += p.lin_b;
          nf.set(k, 1 / (1 + Math.exp(-z)));
        });
        final = nf;
      } else if (p.ranker === "gbdt" && p.gbdt && cand.length) {
        const z = gbdtPredict(p.gbdt, feats);
        const nf = new Map<number, number>();
        cand.forEach((k, i) => nf.set(k, 1 / (1 + Math.exp(-z[i]!))));
        final = nf;
      }
    }
    if (ti.order && final.size && p.ranker === "rrf") {
      let mx = -Infinity;
      for (const v of final.values()) mx = Math.max(mx, v);
      const strong = [...final].filter(([, v]) => v >= 0.5 * mx).map(([k]) => k).slice(0, 30);
      if (strong.length > 1) {
        const tsv = strong.map((k) => P.ts[k]!).sort((a, b) => a - b);
        const lo = tsv[0]!, hi = tsv[tsv.length - 1]!;
        for (const k of strong) {
          const pos = (P.ts[k]! - lo) / (hi - lo + 1e-9);
          const f_ = 1 + p.f_order * (ti.order === "latest" ? pos : 1 - pos);
          final.set(k, final.get(k)! * f_);
          this.multOf(k).set("order", f_);
        }
      }
    }
    if (req.filters.prefer?.length) {
      const pref = req.filters.prefer.map((s) => s.replace(/\/+$/, ""));
      for (const k of final.keys()) {
        const sc = P.mems[k]!.scope;
        if (pref.some((s) => sc === s || sc.startsWith(s + "/"))) {
          final.set(k, final.get(k)! * (1 + p.prefer_boost));
          this.multOf(k).set("prefer", 1 + p.prefer_boost);
        }
      }
    }
    let ranked = [...final.keys()].map((k, i) => [k, i] as const)
      .sort((a, b) => final.get(b[0])! - final.get(a[0])! || a[1] - b[1]).map((x) => x[0]);
    if (p.count_div && (ti.count || ti.span) && ranked.length) {
      ranked = interleaveThreads(ranked.slice(0, 60), P.threadOf).concat(ranked.slice(60));
    }
    const fidx = new Map<number, number>();
    cand.forEach((k, i) => fidx.set(k, i));
    const items = this.pack(ranked, final, chans, req, qterms, mask, feats, fidx, now);
    let cov: number;
    if (p.ranker === "linear" || p.ranker === "gbdt") cov = ranked.length ? final.get(ranked[0]!)! : 0;
    else cov = Math.min(1.0, (ranked.length ? final.get(ranked[0]!)! : 0) / (1 + p.f_cover + p.f_actor + p.f_role));
    if (ti.window) notes.push(`time:${isoDate(ti.window[0])}..${isoDate(ti.window[1])}`);
    return { items, tokens: items.reduce((a, i) => a + i.tokens, 0), budget: req.budget_tokens,
      coverage: pyRound(cov, 3), snapshot: this.snapshot(tenant), notes };
  }

  private multOf(k: number): Map<string, number> {
    let m = this.mult.get(k);
    if (!m) this.mult.set(k, (m = new Map()));
    return m;
  }

  private admissibleNeighbours(P: Partition, mask: Mask): { pm: Int32Array; nm: Int32Array } {
    const pm = new Int32Array(P.n).fill(-1), nm = new Int32Array(P.n).fill(-1);
    P.threadUnits.forEach((ks, t) => {
      if (!P.threadNamed[t]) return;
      const ka = ks.filter((k) => mask[k]);
      for (let i = 1; i < ka.length; i++) {
        pm[ka[i]!] = ka[i - 1]!;
        nm[ka[i - 1]!] = ka[i]!;
      }
    });
    return { pm, nm };
  }

  private fuse(chans: Map<string, Channel>, n: number): Float64Array {
    const out = new Float64Array(n);
    for (const { w, ranks } of chans.values()) {
      if (w <= 0) continue;
      for (const [k, r] of ranks) out[k] = out[k]! + w / (this.p.rrf_k + r + 1);
    }
    return out;
  }

  private featurize(cand: number[], chans: Map<string, Channel>, fused: Float64Array, topFused: number,
    qset: Set<string>, qents: Set<string>, ti: TimeIntent, firstPerson: boolean, asksAssistant: boolean,
    sBm: Float32Array, sDense: Float32Array, ub: Float64Array, ud: Float64Array): Float32Array[] {
    const P = this.P, pm = this.pm, nm = this.nm;
    const X = cand.map(() => new Float32Array(FEATURES.length));
    if (!cand.length) return X;
    const names = ["bm25", "bm25_win", "dense", "dense_win", "bm25_seg", "dense_seg", "thread", "entity", "time", "prf"];
    let bmx = -Infinity, ubx = -Infinity;
    for (const k of cand) { bmx = Math.max(bmx, sBm[k]!); ubx = Math.max(ubx, ub[k]!); }
    bmx = Math.max(bmx, 1e-6); ubx = Math.max(ubx, 1e-6);
    const qn = Math.max(1, qset.size);
    const win = ti.window ? [epochSeconds(ti.window[0]), epochSeconds(ti.window[1])] : null;
    const tsv = cand.map((k) => P.ts[k]!);
    const lo = Math.min(...tsv), hi = Math.max(...tsv);
    const ql = this.qtext.toLowerCase();
    const whereQ = /\bwhere\b|\bwhich (city|country|place|state|town)\b/.test(ql);
    const qentsAll = new Set([...this.qtext.matchAll(CAP_RX)].map((m) => lowerNoS(m[0])));
    const inter = (a: Set<string>, b: Set<string> | null, c?: Set<string> | null) => {
      let h = 0;
      for (const t of a) if ((b && b.has(t)) || (c && c.has(t))) h++;
      return h;
    };
    cand.forEach((k, i) => {
      const x = X[i]!;
      names.forEach((c, j) => {
        const r = chans.get(c)?.ranks.get(k);
        x[j] = r === undefined ? 0 : 1.0 / (1 + r);
      });
      x[10] = fused[k]! / topFused;
      x[11] = sBm[k]! / bmx;
      x[12] = ub[k]! / ubx;
      x[13] = sDense[k]!;
      x[14] = ud[k]!;
      const pk = pm[k]!, nk = nm[k]!;
      const ut = P.unitTerms[k]!;
      const pt = pk >= 0 ? P.unitTerms[pk]! : null;
      const nt = nk >= 0 ? P.unitTerms[nk]! : null;
      x[15] = inter(qset, ut, pt) / qn;
      x[16] = inter(qset, ut) / qn;
      x[17] = inter(qset, nt) / qn;
      const m = P.mems[k]!;
      x[18] = qents.size && qents.has(P.actorL[k]!) ? 1 : 0;
      if (this.hasRolesQ) x[19] = (asksAssistant && m.role === "assistant") || (firstPerson && !asksAssistant && m.role === "user") ? 1 : 0;
      if (win) x[20] = win[0]! <= P.ts[k]! && P.ts[k]! <= win[1]! ? 1 : 0;
      if (ti.order) {
        const pos = (P.ts[k]! - lo) / (hi - lo + 1e-9);
        x[21] = ti.order === "latest" ? pos : 1 - pos;
      }
      x[22] = Math.log1p(P.ntok[k]!);
      x[23] = P.isQ[k] ? 1 : 0;
      x[24] = pk >= 0 && P.isQ[pk] ? 1 : 0;
      x[25] = nk >= 0 && P.actorL[nk] !== P.actorL[k] ? 1 : 0;
      x[26] = ti.when ? 1 : 0;
      x[27] = ti.count || ti.span ? 1 : 0;
      x[28] = Math.max(pk >= 0 ? sBm[pk]! : 0, nk >= 0 ? sBm[nk]! : 0) / bmx;
      x[29] = P.hasTime[k] ? 1 : 0;
      x[30] = P.hasNum[k] ? 1 : 0;
      x[31] = x[26]! * x[29]!;
      x[32] = x[27]! * x[30]!;
      x[33] = (whereQ ? 1 : 0) * (P.hasPlace[k] ? 1 : 0);
      const ents = new Set(((m.meta.entities ?? []) as string[]).map((e) => e.toLowerCase()));
      x[34] = inter(ents, qentsAll) / Math.max(1, qentsAll.size);
      x[35] = x[6]! * x[15]!;
      const rs = chans.get("sub")?.ranks.get(k);
      x[36] = rs === undefined ? 0 : 1.0 / (1 + rs);
      x[37] = this.nParts;
      const rq = chans.get("qexp")?.ranks.get(k);
      x[38] = rq === undefined ? 0 : 1.0 / (1 + rq);
    });
    for (const name of this.p.drop_features) {
      const j = (FEATURES as readonly string[]).indexOf(name);
      for (const x of X) x[j] = 0;
    }
    return X;
  }

  private pack(ranked: number[], final: Map<number, number>, chans: Map<string, Channel>, req: RecallRequest,
    qterms: string[], mask: Mask, feats: Float32Array[] | null, fidx: Map<number, number>, asOf: Micros): ContextItem[] {
    const p = this.p, P = this.P, pm = this.pm, nm = this.nm;
    const gran = req.filters.granularity ?? "auto";
    const nbh = Number(req.filters.neighbours ?? 0) || 0;
    const quotas = req.filters.quotas ?? {};
    const kcount = new Map<string, number>();
    const items: ContextItem[] = [];
    let used = 0;
    const seenThreads = new Set<string>();
    const emitted = new Set<number>();
    for (const k of ranked) {
      if (items.length >= req.max_items) break;
      if (emitted.has(k)) continue;
      const m = P.mems[k]!;
      if (Object.keys(quotas).length && m.kind in quotas && (kcount.get(m.kind) ?? 0) >= Number(quotas[m.kind])) continue;
      let members: number[];
      if (gran === "thread" && P.threadNamed[P.threadOf[k]!]) {
        members = P.threadUnits[P.threadOf[k]!]!.filter((x) => mask[x] && !emitted.has(x));
      } else if (gran === "thread") {
        members = [k];
      } else {
        members = [k];
        const pk = pm[k]!, nk = nm[k]!;
        if (gran !== "unit") {
          const expandPrev = gran === "span" || p.expand_prev_q;
          const expandNext = gran === "span" || p.expand_next;
          if (pk >= 0 && !emitted.has(pk) && P.actorL[pk] !== P.actorL[k] &&
            (m.content.split(/\s+/).filter(Boolean).length <= p.prompt_merge_words || (expandPrev && P.isQ[pk]))) {
            members = [pk, k];
          }
          if (expandNext && nk >= 0 && !emitted.has(nk) && P.actorL[nk] !== P.actorL[k] &&
            (pyRstrip(m.content).endsWith("?") || m.content.slice(-60).includes("?")) &&
            P.ntok[nk]! <= p.long_unit_tokens && (P.mems[nk]!.role !== "assistant" || this.asksAssistant)) {
            members.push(nk);
          }
        }
        for (let s = 0; s < nbh; s++) {
          const a = pm[members[0]!]!, b = nm[members[members.length - 1]!]!;
          if (a >= 0 && !emitted.has(a)) members.unshift(a);
          if (b >= 0 && !emitted.has(b)) members.push(b);
        }
      }
      const lines: string[] = [];
      const claimed: number[] = [];
      for (const j of members) {
        const mj = P.mems[j]!;
        let [body, frac] = gran === "thread" ? [mj.content, 1.0] as [string, number] : this.body(j, qterms);
        const refs = mj.meta.time_refs as Array<[string, string]> | undefined;
        if (p.resolve_dates && refs?.length) body += " (" + refs.slice(0, 2).map(([a, b]) => `${a} = ${b}`).join("; ") + ")";
        let line = mj.actor ? `${mj.actor}: ${body}` : body;
        if (p.render === "clean") {
          const dj = mj.event_time ?? mj.observed_at;
          line = (dj !== null ? `[${strftime(dj, "%-d %b %Y")}] ` : "") + line;
        }
        lines.push(line);
        if (frac >= p.claim_fraction) claimed.push(j);
      }
      let head = "";
      const tkey = m.tenant + "\0" + m.thread;
      if (p.headers && p.render !== "clean" && !seenThreads.has(tkey)) {
        const d = m.event_time ?? m.observed_at;
        const ds = d !== null ? strftime(d, "%Y-%m-%d (%a)") : "";
        const lab = (m.meta.thread_label as string) || (m.thread ? m.thread.split(":").pop()! : "");
        head = lab ? `## ${ds} · ${lab}\n` : `## ${ds}\n`;
      }
      let label: string;
      if (Engine.inChain(m)) label = this.viewSup.has(m.id) ? "superseded" : "current";
      else label = Engine.validAt(m, asOf) ? "current" : (m.status === "superseded" ? "superseded" : "historical");
      let text = head + lines.join("\n");
      if (label !== "current") text = `[${label}] ` + text;
      const tok = countTokens(text);
      if (req.budget_tokens && used + tok > req.budget_tokens) continue; // greedy skip, never exceed (M43)
      if (head) seenThreads.add(tkey);
      for (const j of members) emitted.add(j);
      kcount.set(m.kind, (kcount.get(m.kind) ?? 0) + 1);
      used += tok;
      const why: Record<string, any> = {};
      for (const [c, { w, ranks }] of chans) {
        const r = ranks.get(k);
        if (r !== undefined && w > 0) why[c] = pyRound(w / (p.rrf_k + r + 1), 5);
      }
      if (feats && fidx.has(k) && p.ranker !== "rrf") {
        const fv = feats[fidx.get(k)!]!;
        const fe: Record<string, number> = {};
        FEATURES.forEach((nme, j) => { if (fv[j] && !p.drop_features.includes(nme)) fe[nme] = pyRound(fv[j]!, 4); });
        for (const [kk, vv] of this.mult.get(k) ?? []) if (kk === "prefer" || kk === "order") fe[kk] = pyRound(vv, 4);
        why.features = fe;
      } else {
        const fe: Record<string, number> = {};
        for (const [kk, vv] of this.mult.get(k) ?? []) fe[kk] = pyRound(vv, 4);
        why.features = fe;
      }
      why.ranker = p.ranker;
      const mids = members.map((j) => P.ids[j]!);
      const cites = members.flatMap((j) => P.mems[j]!.sources.map((s) => s.uri));
      items.push({
        memory_ids: mids, text, score: pyRound(final.get(k) ?? 0, 5), why, tokens: tok,
        citations: cites.length ? cites : mids, valid: label,
        turn_ids: claimed.flatMap((j) => {
          const mm = P.mems[j]!;
          return (mm.meta.turn_ids as string[] | undefined)?.length ? mm.meta.turn_ids : [mm.meta.turn_id ?? P.ids[j]];
        }),
      });
    }
    return items;
  }

  private body(k: number, qterms: string[]): [string, number] {
    const P = this.P;
    const m = P.mems[k]!;
    if (P.ntok[k]! <= this.p.long_unit_tokens) return [m.content, 1.0];
    const sents = sentences(m.content);
    if (sents.length <= this.p.snippet_sentences) return [m.content, 1.0];
    const qs = new Set(qterms);
    const scored = sents.map((s, i) => {
      let h = 0;
      for (const t of new Set(terms(s))) if (qs.has(t)) h++;
      return [h + (i === 0 ? 0.5 : 0), i] as [number, number];
    });
    scored.sort((a, b) => b[0] - a[0] || b[1] - a[1]); // sorted(reverse=True) on (score, index)
    const keep = scored.slice(0, this.p.snippet_sentences).map((x) => x[1]).sort((a, b) => a - b);
    const out: string[] = keep[0]! > 0 ? ["…"] : [];
    let last = -1;
    for (const i of keep) {
      if (last >= 0 && i !== last + 1) out.push("…");
      out.push(sents[i]!);
      last = i;
    }
    if (keep[keep.length - 1]! < sents.length - 1) out.push("…");
    const txt = out.join(" ");
    return [txt, txt.length / Math.max(1, m.content.length)];
  }

  /** Per-tenant, keyed: changes on every write, forget, approval or ACL change in this tenant (and on retention
   * expiry), reveals nothing about other tenants (M52). */
  snapshot(tenant = "default"): string {
    const now = nowMicros();
    let exp = 0;
    if (this.hasExpiry) for (const m of this.mem.values()) if (m.tenant === tenant && m.expires_at !== null && m.expires_at <= now) exp++;
    const msg = `${tenant}|${this.tmut.get(tenant) ?? 0}|${this.epoch}|${this.tcount.get(tenant) ?? 0}|${exp}`;
    return createHmac("sha256", this.secret).update(msg).digest("hex").slice(0, 16);
  }
}

function setEq(a: ReadonlySet<string> | null, b: ReadonlySet<string> | null): boolean {
  if (a === null || b === null) return a === b;
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

/** The first `depth` of `ks` ordered by (-score, k), as np.lexsort((k, -score))[:depth]. */
export function topK(ks: number[], score: ArrayLike<number>, depth: number): number[] {
  const cmp = (a: number, b: number) => score[b]! - score[a]! || a - b;
  if (ks.length <= depth * 4 || depth >= ks.length) return [...ks].sort(cmp).slice(0, depth);
  // bounded heap of the current best `depth` (worst at the root)
  const heap: number[] = [];
  const worse = (a: number, b: number) => cmp(a, b) > 0;
  const up = (i: number) => {
    while (i > 0) {
      const pa = (i - 1) >> 1;
      if (worse(heap[i]!, heap[pa]!)) { [heap[i], heap[pa]] = [heap[pa]!, heap[i]!]; i = pa; } else break;
    }
  };
  const down = (i: number) => {
    for (;;) {
      const l = 2 * i + 1, r = l + 1;
      let w = i;
      if (l < heap.length && worse(heap[l]!, heap[w]!)) w = l;
      if (r < heap.length && worse(heap[r]!, heap[w]!)) w = r;
      if (w === i) break;
      [heap[i], heap[w]] = [heap[w]!, heap[i]!];
      i = w;
    }
  };
  for (const k of ks) {
    if (heap.length < depth) { heap.push(k); up(heap.length - 1); } else if (cmp(k, heap[0]!) < 0) { heap[0] = k; down(0); }
  }
  return heap.sort(cmp);
}

function interleaveThreads(ranked: number[], threadOf: number[]): number[] {
  const buckets = new Map<number, number[]>();
  for (const k of ranked) {
    const t = threadOf[k]!;
    let b = buckets.get(t);
    if (!b) buckets.set(t, (b = []));
    b.push(k);
  }
  const out: number[] = [];
  while (buckets.size) {
    for (const [t, b] of [...buckets]) {
      out.push(b.shift()!);
      if (!b.length) buckets.delete(t);
    }
  }
  return out;
}

export function gbdtPredict(model: GbdtModel, X: Float32Array[]): number[] {
  return X.map((x) => {
    let out = Number(model.base);
    for (const t of model.trees) {
      let node = 0;
      for (let s = 0; s < 64 && !t.leaf[node]; s++) node = x[t.f[node]!]! <= t.t[node]! ? t.l[node]! : t.r[node]!;
      out += t.v[node]!;
    }
    return out;
  });
}

export { Principal };
