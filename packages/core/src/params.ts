// Engine parameters (port of orp.engine.Params) and the shipped rankers, which are data (spec §5.10, M53).
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

export interface Params {
  w_bm25: number; w_bm25_win: number; w_dense: number; w_dense_win: number; w_thread: number; w_entity: number;
  w_time: number; w_prf: number; w_sub: number; w_qexp: number; qexp_weight: number; qexp_min: number;
  w_bm25_seg: number; w_dense_seg: number; seg_sentences: number; rrf_k: number; depth: number; pool: number;
  f_cover: number; f_actor: number; f_role: number; f_order: number; prefer_boost: number; prf_docs: number;
  prf_terms: number; prf_mix: number; long_unit_tokens: number; snippet_sentences: number; claim_fraction: number;
  prompt_merge_words: number; expand_next: boolean; expand_prev_q: boolean; headers: boolean; render: string;
  resolve_dates: boolean; count_div: boolean; ranker: string; lin_w: number[]; lin_b: number; lin_mu: number[];
  lin_sd: number[]; gbdt: GbdtModel | null; drop_features: string[]; admissible_stats: boolean;
}

export interface GbdtModel {
  base: number;
  trees: Array<{ f: number[]; t: number[]; l: number[]; r: number[]; v: number[]; leaf: number[] }>;
}

export function defaultParams(): Params {
  return {
    w_bm25: 1.0, w_bm25_win: 0.6, w_dense: 0.6, w_dense_win: 0.4, w_thread: 0.5, w_entity: 0.4, w_time: 0.5,
    w_prf: 0.6, w_sub: 0.0, w_qexp: 0.0, qexp_weight: 0.4, qexp_min: 0.45, w_bm25_seg: 0.6, w_dense_seg: 0.4,
    seg_sentences: 2, rrf_k: 60.0, depth: 200, pool: 300, f_cover: 0.6, f_actor: 0.25, f_role: 0.25, f_order: 0.15,
    prefer_boost: 0.25, prf_docs: 8, prf_terms: 12, prf_mix: 0.5, long_unit_tokens: 220, snippet_sentences: 4,
    claim_fraction: 0.0, prompt_merge_words: 14, expand_next: true, expand_prev_q: true, headers: true,
    render: "clean", resolve_dates: true, count_div: true, ranker: "rrf", lin_w: [], lin_b: 0.0, lin_mu: [],
    lin_sd: [], gbdt: null, drop_features: [], admissible_stats: true,
  };
}

export const FEATURES = ["rr_bm25", "rr_bm25_win", "rr_dense", "rr_dense_win", "rr_bm25_seg", "rr_dense_seg",
  "rr_thread", "rr_entity", "rr_time", "rr_prf", "fused", "bm25_norm", "bm25_seg_norm", "cos", "cos_seg", "cover",
  "cover_unit", "cover_next", "actor_match", "role_match", "in_window", "order_pos", "log_tokens", "is_question",
  "prev_is_question", "next_reply", "when_q", "count_q", "nb_bm25", "has_time", "has_number", "when_x_time",
  "count_x_number", "where_x_place", "ent_overlap", "rr_thread_x_cover", "rr_sub", "n_parts", "rr_qexp"] as const;

export const RANKERS = ["orp-linear-1", "orp-rrf-1"] as const;

export interface RankerSpec {
  name: string; kind: string; fit_encoder: string | null; fitted_on: string; selection?: string;
  params: Partial<Params>;
}

const HERE = dirname(fileURLToPath(import.meta.url));

export function rankerSpec(name: string): RankerSpec {
  if (!(RANKERS as readonly string[]).includes(name)) {
    throw new Error(`unknown ranker ${JSON.stringify(name)}; shipped: ${RANKERS.join(", ")}`);
  }
  return JSON.parse(readFileSync(join(HERE, "..", "rankers", name + ".json"), "utf8")) as RankerSpec;
}

export function withParams(base: Params, over: Partial<Params>): Params {
  const out = { ...base };
  for (const [k, v] of Object.entries(over)) {
    if (!(k in base)) throw new Error(`unknown parameter ${k}`);
    (out as any)[k] = Array.isArray(v) ? [...v] : v;
  }
  return out;
}

export function rankerParams(name: string): Params {
  return withParams(defaultParams(), rankerSpec(name).params);
}

/** auto: the learned ranker only with the encoder it was fit with, otherwise RRF (and say why). */
export function chooseRanker(name: string | undefined | null, encoderName: string):
  { name: string; params: Params; notes: string[] } {
  const notes: string[] = [];
  let n = name ?? "auto";
  if (n === "" || n === "auto") {
    n = encoderName === rankerSpec("orp-linear-1").fit_encoder ? "orp-linear-1" : "orp-rrf-1";
    if (n === "orp-rrf-1") {
      notes.push(`ranker auto: encoder ${encoderName || "unknown"} is not the one orp-linear-1 was fit with, so ` +
        "orp-rrf-1 is used");
    }
  }
  return { name: n, params: rankerParams(n), notes };
}
