// Model-backed extraction (spec §4.6 reflect, kinds fact/preference/procedure): turn one raw message into durable
// memories, and replace the ones it changes. Uses only public verbs (get, recall, remember), so every access rule
// applies to it exactly as to any other caller: it reads what the caller may read and writes as the caller.
import { createHash } from "node:crypto";
import type { ChatModel, ModelUsage } from "./llm.js";
import type { Principal } from "./model.js";

type Json = Record<string, any>;

/** The verbs extraction uses. Service implements them. */
export interface ExtractionHost {
  get(ids: unknown, pr: Principal): Promise<Json>;
  recall(body: unknown, pr: Principal): Promise<Json>;
  remember(body: unknown, pr: Principal): Promise<Json>;
}

export const EXTRACTION_KINDS = ["fact", "preference", "procedure"] as const;
export type ExtractionKind = (typeof EXTRACTION_KINDS)[number];

export interface ExtractionResult {
  source: string;
  created: string[];
  replaced: string[];
  /** Drafts that would replace a memory a person wrote or approved: they wait for approval (spec §4.6). */
  proposed: string[];
  usage: ModelUsage[];
  skipped?: string;
}

export const EXTRACT_PROMPT = `You maintain the long-term memory of a personal assistant. You read ONE message and decide what, if anything, should be remembered for later: weeks, months or years from now, when the message itself is long out of view.

Remember only what will still matter later:
- procedure: a standing rule, habit or instruction for how things should be done ("send invoices on the 1st", "only post release notes in #eng-releases until the deploy is green", "never cc my manager on vendor threads"). Keep every condition, exception, channel, person, recipient, format and time limit exactly; these details are the point.
- preference: a lasting like, dislike or default choice ("prefers morning meetings", "wants summaries as bullet points").
- fact: a durable fact about the people, projects, accounts, places, commitments, deadlines and decisions in the user's life ("Dana owns the billing migration", "the Q3 offsite is in Lisbon on 12-14 Sept 2026").
Do not store: small talk, one-off requests that are done once handled, the assistant's own suggestions, or anything already stored with the same meaning.

Write each memory as one self-contained sentence that makes sense with no other context: name the people and things (the user is "{{speaker}}"), and turn relative dates into absolute ones using the message date. Keep the user's own wording for names, channels, addresses and identifiers.

EXISTING memories related to this message are listed with ids. If the message changes, corrects, narrows, extends, ends or revokes one of them, output a "replace" of that id whose content states what holds NOW (for a revoked rule: that it no longer applies, and what applies instead if anything). Never repeat an existing memory unchanged.

Reply with JSON only: {"memories": [{"op": "add" | "replace", "replaces": "<existing id, for replace>", "kind": "procedure" | "preference" | "fact", "content": "<one sentence>", "subject": "<optional: who or what a fact or preference is about>", "predicate": "<optional: the attribute, for a fact or preference with one current value>", "object": "<optional: that value>"}]}
Use {"memories": []} when nothing should be remembered. Most messages need nothing.`;

const MAX_PER_MESSAGE = 8;

function iso(t: unknown): string | null {
  if (typeof t === "string" && t) return t;
  if (t && typeof t === "object" && typeof (t as Json).start === "string") return (t as Json).start;
  return null;
}

/** Extract durable memories from one stored message (an event or document) and write them as the caller. Rules
 * and preferences are standing context (load: on_scope). Extraction from a message whose role is not user or human
 * writes facts only: what an assistant or tool said is not the user's rule or preference. */
export async function extractFrom(host: ExtractionHost, pr: Principal, sourceId: string, model: ChatModel,
  kinds: readonly ExtractionKind[] = EXTRACTION_KINDS): Promise<ExtractionResult> {
  const got = await host.get([sourceId], pr);
  const src = (got.memories as Json[])[0];
  if (!src) return { source: sourceId, created: [], replaced: [], proposed: [], usage: [], skipped: "not_found" };
  if ((src.kind !== "event" && src.kind !== "document") || src.status !== "active") {
    return { source: sourceId, created: [], replaced: [], proposed: [], usage: [], skipped: "not an active event or document" };
  }
  const userStated = src.role === "user" || src.role === "human";
  const allowed = new Set<string>(kinds.filter((k) => userStated || k === "fact"));
  if (!allowed.size) return { source: sourceId, created: [], replaced: [], proposed: [], usage: [], skipped: "no kinds apply" };
  const when = iso(src.event_time) ?? iso(src.valid_from) ?? iso(src.observed_at);
  const content = String(src.content);

  // existing memories this message may change, as they stood when it was said
  const existing = new Map<string, Json>();
  const rec = await host.recall({ query: content.slice(0, 2000), ...(when ? { as_of: when } : {}), kinds: [...EXTRACTION_KINDS],
    budget: { tokens: 1200, items: 15, standing: 0 }, granularity: "unit", include: { explain: false } }, pr);
  for (const it of rec.items as Json[]) {
    const id = it.memory_ids?.[0];
    if (typeof id === "string" && !existing.has(id)) existing.set(id, { id, kind: it.kind, text: it.text });
  }
  // a memory a person wrote or approved (origin: human) is never replaced by the model on its own: the change is
  // written as a proposal that supersedes it once approved
  const human = new Set<string>();
  if (existing.size) {
    const cur = await host.get([...existing.keys()], pr);
    for (const m of (cur.memories ?? []) as Json[]) if (m?.origin === "human") human.add(String(m.id));
  }
  const speaker = String(src.actor || "the user");
  const system = EXTRACT_PROMPT.replaceAll("{{speaker}}", speaker);
  const user = JSON.stringify({ message_date: when, speaker, role: src.role || null, message: content,
    existing: [...existing.values()] });
  const out = await model.complete([{ role: "system", content: system }, { role: "user", content: user }], { json: true, maxTokens: 1500 });
  const usage = [out.usage];

  let ops: Json[] = [];
  try {
    const parsed = JSON.parse(stripFence(out.text));
    if (Array.isArray(parsed?.memories)) ops = parsed.memories;
  } catch {
    return { source: sourceId, created: [], replaced: [], proposed: [], usage, skipped: "model reply was not JSON" };
  }
  const created: string[] = [], replaced: string[] = [], proposed: string[] = [];
  let n = 0;
  for (const op of ops) {
    if (n >= MAX_PER_MESSAGE) break;
    if (!op || typeof op !== "object") continue;
    const kind = String(op.kind ?? "");
    const text = typeof op.content === "string" ? op.content.trim() : "";
    if (!allowed.has(kind) || !text || text.length > 2000) continue;
    const isReplace = op.op === "replace" && typeof op.replaces === "string" && existing.has(op.replaces);
    const m: Json = { kind, content: text, origin: "extracted", derived_from: [sourceId], readers: src.readers ?? null,
      load: kind === "fact" ? "on_query" : "on_scope", meta: { via: "extract", model: out.usage.model } };
    if (src.scope) m.scope = src.scope;
    if (when) { m.event_time = when; m.valid_from = when; }
    if (kind !== "procedure" && [op.subject, op.predicate, op.object].every((x) => typeof x === "string" && x.trim())) {
      m.claim = { subject: op.subject.trim(), predicate: op.predicate.trim(), object: op.object.trim(), cardinality: "one" };
    }
    if (isReplace) m.supersedes = [op.replaces];
    const key = "extract:" + createHash("sha256").update(`${sourceId}|${n}|${kind}|${text}`).digest("hex").slice(0, 24);
    const propose = isReplace && human.has(op.replaces);
    try {
      const r = await host.remember({ memories: [m], mode: propose ? "propose" : isReplace ? "supersede" : "append",
        idempotency_key: key }, pr);
      if (propose) proposed.push(...(r.ids as string[]));
      else created.push(...(r.ids as string[]));
      if (isReplace && !propose) replaced.push(op.replaces);
      n++;
    } catch {
      // one bad memory (for example a replace the caller may not write) never loses the others
    }
  }
  return { source: sourceId, created, replaced, proposed, usage };
}

function stripFence(s: string): string {
  const t = s.trim();
  const m = /^```(?:json)?\s*([\s\S]*?)\s*```$/.exec(t);
  return m ? m[1]! : t;
}
