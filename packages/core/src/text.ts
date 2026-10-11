// Shared text utilities: tokeniser, stemmer, stopwords, token counting, sentence split.
// A line-for-line port of orp/_text.py. Any change here changes recall, so the oracle is the test.

const WORD = /[a-z0-9]+(?:'[a-z]+)?/g;
// Python's \w is Unicode-aware (letters, digits, underscore); JavaScript's is not, so spell it out.
const TOKCOUNT = /[\p{L}\p{N}_]+|[^\p{L}\p{N}_\s]/gu;

export const STOPWORDS: ReadonlySet<string> = new Set(
  `a about above after again against all am an and any are aren't as at be because been before being
    below between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during
    each few for from further had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers
    herself him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself let's me more most
    mustn't my myself no nor not of off on once only or other ought our ours ourselves out over own same shan't she
    she'd she'll she's should shouldn't so some such than that that's the their theirs them themselves then there
    there's these they they'd they'll they're they've this those through to too under until up very was wasn't we
    we'd we'll we're we've were weren't what what's when when's where where's which while who who's whom why why's
    with won't would wouldn't you you'd you'll you're you've your yours yourself yourselves also just really yeah
    oh hey hi okay ok so um uh like well get got gonna wanna lot lots thing things`.split(/\s+/).filter(Boolean),
);

const SUFFIXES: ReadonlyArray<[string, string]> = [
  ["ational", "ate"], ["ization", "ize"], ["fulness", "ful"], ["iveness", "ive"], ["ousness", "ous"], ["ies", "y"],
  ["sses", "ss"], ["ness", ""], ["ments", "ment"], ["ingly", ""], ["edly", ""], ["ing", ""], ["ed", ""], ["ly", ""],
  ["es", "e"], ["s", ""],
];

const stemCache = new Map<string, string>();

function stemUncached(w0: string): string {
  let w = w0;
  if (w.length <= 3 || /^[0-9]+$/.test(w)) return w;
  if (w.endsWith("'s")) w = w.slice(0, -2);
  for (const [suf, rep] of SUFFIXES) {
    if (w.endsWith(suf) && w.length - suf.length >= 3) {
      let base = w.slice(0, w.length - suf.length) + rep;
      if ((suf === "ing" || suf === "ed") && base.length > 2 && base[base.length - 1] === base[base.length - 2] &&
        !"lsz".includes(base[base.length - 1]!)) {
        base = base.slice(0, -1); // running -> run
      }
      if (suf === "s" && w.endsWith("ss")) return w;
      return base;
    }
  }
  return w;
}

/** Small deterministic suffix stripper (Porter-lite), shared by every system in the benchmark. */
export function stem(w: string): string {
  let s = stemCache.get(w);
  if (s === undefined) {
    s = stemUncached(w);
    if (stemCache.size > 200_000) stemCache.clear();
    stemCache.set(w, s);
  }
  return s;
}

/** Python str.lower() and String.toLowerCase() agree for the characters the word regex can match. */
export function words(text: string): string[] {
  return text.toLowerCase().match(WORD) ?? [];
}

/** BM25 terms: lowercase, stopwords removed, stemmed. */
export function terms(text: string): string[] {
  const out: string[] = [];
  for (const w of words(text)) if (!STOPWORDS.has(w)) out.push(stem(w));
  return out;
}

/** Approximate LLM token count (word pieces + punctuation), about 1.25 per English word. */
export function countTokens(text: string): number {
  const n = (text.match(TOKCOUNT) ?? []).length;
  return Math.max(1, Math.trunc(n * 1.1));
}

/** Python str.strip(): whitespace per str.isspace (close enough to JS trim for the separators we meet). */
export function pyStrip(s: string): string {
  return s.trim();
}

export function sentences(text: string): string[] {
  const parts = pyStrip(text).split(/(?<=[.!?])\s+(?=[A-Z0-9"'])|\n+/);
  const out: string[] = [];
  for (const p of parts) {
    if (p === undefined) continue;
    const s = pyStrip(p);
    if (s.length > 1) out.push(s);
  }
  return out;
}

export function counter(xs: Iterable<string>): Map<string, number> {
  const c = new Map<string, number>();
  for (const x of xs) c.set(x, (c.get(x) ?? 0) + 1);
  return c;
}
