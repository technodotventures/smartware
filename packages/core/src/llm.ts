// The Model port (spec §4.6): the chat model a server uses for model-backed reflect, such as extraction. Any
// OpenAI-compatible chat completions endpoint (OpenAI, OpenRouter, DeepSeek, a local server). No SDK dependency.

export interface ModelUsage {
  model: string;
  input_tokens: number;
  /** Of input_tokens, those served from the provider's prompt cache (billed at the cache rate), when reported. */
  cached_input_tokens: number;
  output_tokens: number;
  /** Provider-reported cost in USD when the provider reports it (OpenRouter does), otherwise null. */
  cost_usd: number | null;
}

export interface ChatMessage { role: "system" | "user" | "assistant"; content: string }

export interface ChatModel {
  /** Declared in capabilities and on every job that used it. */
  readonly name: string;
  complete(messages: ChatMessage[], opts?: { json?: boolean; maxTokens?: number }): Promise<{ text: string; usage: ModelUsage }>;
}

export interface OpenAiCompatibleOptions {
  /** Base URL up to /v1, for example https://openrouter.ai/api/v1. */
  baseUrl: string;
  model: string;
  apiKey?: string;
  timeoutMs?: number;
  /** Attempts on 429 and 5xx before giving up (default 5). */
  attempts?: number;
  fetchImpl?: typeof fetch;
  /** Extra request fields the provider needs, for example {"thinking": {"type": "disabled"}} for DeepSeek. */
  extraBody?: Record<string, unknown>;
}

/** A ChatModel over an OpenAI-compatible /chat/completions endpoint, temperature 0. */
export function openAiCompatible(o: OpenAiCompatibleOptions): ChatModel {
  const base = o.baseUrl.replace(/\/+$/, "");
  const doFetch = o.fetchImpl ?? fetch;
  const openRouter = /openrouter\.ai/.test(base);
  return {
    name: `openai-compatible:${o.model}@${base}`,
    async complete(messages, opts = {}) {
      const body: Record<string, unknown> = { ...(o.extraBody ?? {}), model: o.model, temperature: 0, messages, max_tokens: opts.maxTokens ?? 1200 };
      if (opts.json) body.response_format = { type: "json_object" };
      if (openRouter) body.usage = { include: true }; // OpenRouter then reports the charged cost
      const attempts = o.attempts ?? 5;
      for (let a = 0; ; a++) {
        let r: Response;
        try {
          r = await doFetch(`${base}/chat/completions`, {
            method: "POST", signal: AbortSignal.timeout(o.timeoutMs ?? 120_000),
            headers: { "content-type": "application/json", ...(o.apiKey ? { authorization: `Bearer ${o.apiKey}` } : {}) },
            body: JSON.stringify(body),
          });
        } catch (e) {
          if (a + 1 >= attempts) throw new Error(`model API unreachable: ${String(e)}`);
          await backoff(a);
          continue;
        }
        if (r.ok) {
          const b = (await r.json()) as Record<string, any>;
          const u = b.usage ?? {};
          return {
            text: String(b.choices?.[0]?.message?.content ?? ""),
            usage: { model: String(b.model ?? o.model), input_tokens: Number(u.prompt_tokens ?? 0),
              cached_input_tokens: Number(u.prompt_tokens_details?.cached_tokens ?? u.prompt_cache_hit_tokens ?? 0),
              output_tokens: Number(u.completion_tokens ?? 0),
              cost_usd: typeof u.cost === "number" ? u.cost : null },
          };
        }
        if (r.status === 400 || r.status === 401 || r.status === 402 || r.status === 403 || r.status === 404) {
          throw new Error(`the model API refused the request (${r.status}): check the model name, key and credit`);
        }
        if (a + 1 >= attempts) throw new Error(`model API error ${r.status}`);
        await backoff(a);
      }
    },
  };
}

const backoff = (a: number) => new Promise((ok) => setTimeout(ok, 2 ** a * 500 * (0.5 + Math.random())));

/** Parse "openai:<model>@<base-url>" (the same form as smartware eval --reader). */
export function chatModelFrom(spec: string, apiKey: string | undefined, extraBody?: Record<string, unknown>): ChatModel {
  if (!spec.startsWith("openai:")) throw new Error(`unknown model ${spec}: use openai:<model>@<base-url>`);
  const rest = spec.slice(7);
  const at = rest.lastIndexOf("@");
  const model = at < 0 ? rest : rest.slice(0, at);
  const baseUrl = at < 0 ? "https://api.openai.com/v1" : rest.slice(at + 1);
  if (!model) throw new Error(`no model name in ${spec}`);
  return openAiCompatible({ baseUrl, model, apiKey, ...(extraBody ? { extraBody } : {}) });
}
