// A small JSON Schema subset: enough to describe and check the memory tools' inputs without a dependency.
// Unknown fields are rejected (unsupported_parameter), wrong types are invalid_parameter, as at the gateway.
import { OrpError } from "@orp/client";

export interface JsonSchema {
  type?: "object" | "string" | "integer" | "number" | "boolean" | "array";
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean;
  items?: JsonSchema;
  enum?: readonly string[];
  minimum?: number;
  maximum?: number;
  maxLength?: number;
  minItems?: number;
  default?: unknown;
}

export function validate(value: unknown, s: JsonSchema, where: string): void {
  const bad = (msg: string) => { throw new OrpError("invalid_parameter", `${where}: ${msg}`); };
  switch (s.type) {
    case "object": {
      if (value === null || typeof value !== "object" || Array.isArray(value)) bad("expected an object");
      const o = value as Record<string, unknown>;
      const props = s.properties ?? {};
      const extra = Object.keys(o).filter((k) => !(k in props));
      if (extra.length) throw new OrpError("unsupported_parameter", `${where}: unsupported parameter(s) ${extra.join(", ")}`, 0, { parameters: extra });
      for (const k of s.required ?? []) if (o[k] === undefined) bad(`${k} is required`);
      for (const [k, sub] of Object.entries(props)) if (o[k] !== undefined) validate(o[k], sub, `${where}.${k}`);
      return;
    }
    case "string":
      if (typeof value !== "string") bad("expected a string");
      if (s.maxLength !== undefined && (value as string).length > s.maxLength) bad(`longer than ${s.maxLength}`);
      break;
    case "integer":
    case "number":
      if (typeof value !== "number" || !Number.isFinite(value)) bad("expected a number");
      if (s.type === "integer" && !Number.isInteger(value)) bad("expected an integer");
      if (s.minimum !== undefined && (value as number) < s.minimum) bad(`must be >= ${s.minimum}`);
      if (s.maximum !== undefined && (value as number) > s.maximum) bad(`must be <= ${s.maximum}`);
      break;
    case "boolean":
      if (typeof value !== "boolean") bad("expected a boolean");
      break;
    case "array":
      if (!Array.isArray(value)) bad("expected an array");
      if (s.minItems !== undefined && (value as unknown[]).length < s.minItems) bad(`needs at least ${s.minItems} item(s)`);
      (value as unknown[]).forEach((v, i) => validate(v, s.items ?? {}, `${where}[${i}]`));
      return;
  }
  if (s.enum && !s.enum.includes(value as string)) {
    throw new OrpError("unsupported_parameter", `${where}: ${JSON.stringify(value)} is not one of ${s.enum.join(", ")}`);
  }
}

/** The structural part of Zod this kit uses. Pass your own `z` (from "zod"); nothing here imports it. */
export interface ZodLike {
  object(shape: Record<string, any>): any;
  string(): any;
  number(): any;
  boolean(): any;
  array(item: any): any;
  enum(values: [string, ...string[]]): any;
}

/** Convert one of the tools' schemas to a Zod schema, for hosts that only accept Zod. */
export function toZod(s: JsonSchema, z: ZodLike): any {
  let out: any;
  switch (s.type) {
    case "object": {
      out = z.object(toZodShape(s, z));
      if (s.additionalProperties === false && typeof out.strict === "function") out = out.strict();
      break;
    }
    case "string":
      out = s.enum ? z.enum([...s.enum] as [string, ...string[]]) : z.string();
      if (!s.enum && s.maxLength !== undefined) out = out.max(s.maxLength);
      break;
    case "integer":
    case "number":
      out = z.number();
      if (s.type === "integer") out = out.int();
      if (s.minimum !== undefined) out = out.min(s.minimum);
      if (s.maximum !== undefined) out = out.max(s.maximum);
      break;
    case "boolean": out = z.boolean(); break;
    case "array":
      out = z.array(toZod(s.items ?? { type: "string" }, z));
      if (s.minItems !== undefined) out = out.min(s.minItems);
      break;
    default: throw new Error(`toZod: unsupported schema type ${String(s.type)}`);
  }
  return s.description ? out.describe(s.description) : out;
}

/** A Zod raw shape ({ field: zodType }) for an object schema; optional fields are .optional(). */
export function toZodShape(s: JsonSchema, z: ZodLike): Record<string, any> {
  const shape: Record<string, any> = {};
  const req = new Set(s.required ?? []);
  for (const [k, sub] of Object.entries(s.properties ?? {})) {
    const t = toZod(sub, z);
    shape[k] = req.has(k) ? t : t.optional();
  }
  return shape;
}
