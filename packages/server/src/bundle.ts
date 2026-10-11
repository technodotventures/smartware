// ORP bundles on disk (spec §8.3): a directory or a `.orp` zip holding manifest.json, memories.jsonl, edges.jsonl,
// audit.jsonl and an optional render/ of Markdown. Bundles are data: nothing in one is evaluated, paths inside a zip
// are checked, and sizes are capped. The zip reader and writer are small and dependency-free (deflate from node:zlib).
import { createHash } from "node:crypto";
import { lstatSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { crc32, deflateRawSync, inflateRawSync } from "node:zlib";
import { Principal, type Service } from "@orp/core";

type Json = Record<string, any>;
export type BundleFiles = Map<string, Buffer>;

/** Largest bundle read, uncompressed (all entries together). Larger tenants move with `orp backup` or store tools. */
export const MAX_BUNDLE_BYTES = 512 * 2 ** 20;
const MAX_ENTRIES = 1_000_000;

const sha256 = (b: Buffer | string) => createHash("sha256").update(b).digest("hex");

function safeName(name: string): string {
  const n = name.replace(/\\/g, "/");
  if (!n || n.startsWith("/") || /^[a-zA-Z]:/.test(n) || n.split("/").some((p) => p === ".." || p === "") || n.includes("\0")) {
    throw new Error(`unsafe path in bundle: ${JSON.stringify(name)}`);
  }
  return n;
}

// ------------------------------------------------------------ zip (stored or deflated entries, no zip64)
export function writeZip(files: BundleFiles): Buffer {
  const local: Buffer[] = [], central: Buffer[] = [];
  let off = 0;
  for (const [name0, data] of [...files].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    const name = Buffer.from(safeName(name0), "utf8");
    const comp = deflateRawSync(data, { level: 9 });
    const useDeflate = comp.length < data.length;
    const body = useDeflate ? comp : data;
    const crc = crc32(data) >>> 0;
    if (off > 0xfffffff0 || body.length > 0xfffffff0) throw new Error("bundle too large for a zip without zip64");
    const h = Buffer.alloc(30);
    h.writeUInt32LE(0x04034b50, 0); h.writeUInt16LE(20, 4); h.writeUInt16LE(0x0800, 6); // UTF-8 names
    h.writeUInt16LE(useDeflate ? 8 : 0, 8); h.writeUInt16LE(0, 10); h.writeUInt16LE(0x21, 12); // fixed 1980-01-01 time
    h.writeUInt32LE(crc, 14); h.writeUInt32LE(body.length, 18); h.writeUInt32LE(data.length, 22);
    h.writeUInt16LE(name.length, 26); h.writeUInt16LE(0, 28);
    const c = Buffer.alloc(46);
    c.writeUInt32LE(0x02014b50, 0); c.writeUInt16LE(20, 4); c.writeUInt16LE(20, 6); c.writeUInt16LE(0x0800, 8);
    c.writeUInt16LE(useDeflate ? 8 : 0, 10); c.writeUInt16LE(0, 12); c.writeUInt16LE(0x21, 14);
    c.writeUInt32LE(crc, 16); c.writeUInt32LE(body.length, 20); c.writeUInt32LE(data.length, 24);
    c.writeUInt16LE(name.length, 28); c.writeUInt32LE(off, 42);
    local.push(h, name, body);
    central.push(c, name);
    off += h.length + name.length + body.length;
  }
  const cd = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(files.size, 8); end.writeUInt16LE(files.size, 10);
  end.writeUInt32LE(cd.length, 12); end.writeUInt32LE(off, 16);
  if (files.size > 0xffff) throw new Error("too many files for a zip without zip64");
  return Buffer.concat([...local, cd, end]);
}

export function readZip(buf: Buffer, maxBytes = MAX_BUNDLE_BYTES): BundleFiles {
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 22 - 0xffff); i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("not a zip file (no end of central directory)");
  const n = buf.readUInt16LE(eocd + 10), cdOff = buf.readUInt32LE(eocd + 16);
  if (n > MAX_ENTRIES) throw new Error("too many entries");
  const out: BundleFiles = new Map();
  const spans: Array<[number, number]> = [];
  let p = cdOff, total = 0;
  for (let k = 0; k < n; k++) {
    if (p + 46 > buf.length || buf.readUInt32LE(p) !== 0x02014b50) throw new Error("corrupt zip central directory");
    const method = buf.readUInt16LE(p + 10), crc = buf.readUInt32LE(p + 16), csize = buf.readUInt32LE(p + 20);
    const usize = buf.readUInt32LE(p + 24), nlen = buf.readUInt16LE(p + 28), xlen = buf.readUInt16LE(p + 30);
    const clen = buf.readUInt16LE(p + 32), loff = buf.readUInt32LE(p + 42);
    const name = buf.subarray(p + 46, p + 46 + nlen).toString("utf8");
    p += 46 + nlen + xlen + clen;
    if (name.endsWith("/")) continue; // directory entry
    const safe = safeName(name);
    total += usize;
    if (usize > maxBytes || total > maxBytes) throw new Error(`bundle is over ${maxBytes} bytes uncompressed`);
    if (loff + 30 > buf.length || buf.readUInt32LE(loff) !== 0x04034b50) throw new Error("corrupt zip local header");
    const start = loff + 30 + buf.readUInt16LE(loff + 26) + buf.readUInt16LE(loff + 28);
    if (start + csize > cdOff) throw new Error("corrupt zip: an entry runs into the central directory");
    for (const [a, b] of spans) if (loff < b && start + csize > a) throw new Error("corrupt zip: entries overlap"); // no quoted-overlap bombs
    spans.push([loff, start + csize]);
    const raw = buf.subarray(start, start + csize);
    let data: Buffer;
    if (method === 0) data = Buffer.from(raw);
    else if (method === 8) data = inflateRawSync(raw, { maxOutputLength: usize + 1 });
    else throw new Error(`unsupported zip compression method ${method}`);
    if (data.length !== usize || (crc32(data) >>> 0) !== crc) throw new Error(`checksum mismatch in ${safe}`);
    if (out.has(safe)) throw new Error(`duplicate entry ${safe}`);
    out.set(safe, data);
  }
  return out;
}

// ------------------------------------------------------------ directory or zip
export function writeBundleFiles(out: string, files: BundleFiles): string {
  const path = resolve(out);
  if (/\.(orp|zip)$/i.test(path)) {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, writeZip(files), { mode: 0o600 });
    return path;
  }
  for (const [name, data] of files) {
    const target = join(path, safeName(name));
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, data, { mode: 0o600 });
  }
  return path;
}

export function readBundleFiles(src: string, maxBytes = MAX_BUNDLE_BYTES): BundleFiles {
  const path = resolve(src);
  const st = statSync(path);
  if (st.isFile()) {
    if (st.size > maxBytes) throw new Error(`bundle is over ${maxBytes} bytes`);
    return readZip(readFileSync(path), maxBytes);
  }
  const out: BundleFiles = new Map();
  let total = 0;
  const walk = (dir: string) => {
    for (const e of readdirSync(dir)) {
      const full = join(dir, e);
      const ls = lstatSync(full);
      if (ls.isSymbolicLink()) throw new Error(`symbolic link in bundle directory: ${full}`); // data only, never a pointer
      if (ls.isDirectory()) walk(full);
      else if (ls.isFile()) {
        total += ls.size;
        if (total > maxBytes) throw new Error(`bundle is over ${maxBytes} bytes`);
        out.set(safeName(relative(path, full).split(sep).join("/")), readFileSync(full));
      }
    }
  };
  walk(path);
  return out;
}

// ------------------------------------------------------------ export / import against a service
/** The operator of a store acts as the tenant's administrator for export and import. */
export const operator = (tenant: string) => new Principal("operator", ["*", "auditor", "label:secret"], tenant);

export interface ExportOptions { render?: boolean; audit?: boolean }

export async function exportBundle(svc: Service, tenant: string, opts: ExportOptions = {}): Promise<BundleFiles> {
  const pr = operator(tenant);
  const ex = await svc.export(pr);
  const caps = await svc.capabilities(pr);
  const files: BundleFiles = new Map();
  const mem = Buffer.from(ex["memories.jsonl"] ? ex["memories.jsonl"] + "\n" : "", "utf8");
  const edges = Buffer.from(ex["edges.jsonl"] ? ex["edges.jsonl"] + "\n" : "", "utf8");
  files.set("memories.jsonl", mem);
  files.set("edges.jsonl", edges);
  let auditCount = 0;
  if (opts.audit !== false) {
    const entries = (await svc.auditLog(pr)).entries as Json[];
    auditCount = entries.length;
    files.set("audit.jsonl", Buffer.from(entries.map((e) => JSON.stringify(e)).join("\n") + (entries.length ? "\n" : ""), "utf8"));
  }
  if (opts.render) for (const [name, md] of renderMarkdown(ex["memories.jsonl"])) files.set(name, Buffer.from(md, "utf8"));
  const { request_id: _r, degraded: _d, ...capsClean } = caps;
  const manifest = {
    ...ex.manifest,
    format: "orp-bundle-1",
    counts: { memories: ex.manifest.count, edges: ex["edges.jsonl"] ? ex["edges.jsonl"].split("\n").length : 0, audit: auditCount,
      tombstones: ex.manifest.tombstones.length },
    files: Object.fromEntries([...files].sort(([a], [b]) => (a < b ? -1 : 1)).map(([n, b]) => [n, { sha256: sha256(b), bytes: b.length }])),
    capabilities: capsClean,
  };
  files.set("manifest.json", Buffer.from(JSON.stringify(manifest, null, 2) + "\n", "utf8"));
  return files;
}

/** Check a bundle's file hashes against its manifest (files the manifest does not list are ignored on import). */
export function verifyBundle(files: BundleFiles): Json {
  const raw = files.get("manifest.json");
  if (!raw) throw new Error("bundle has no manifest.json");
  let manifest: Json;
  try { manifest = JSON.parse(raw.toString("utf8")); } catch { throw new Error("manifest.json is not valid JSON"); }
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) throw new Error("manifest.json is not an object");
  const listed = (manifest.files ?? {}) as Json;
  for (const need of ["memories.jsonl", "edges.jsonl"]) {
    if (typeof listed[need]?.sha256 !== "string") throw new Error(`manifest.json must list ${need} with its sha256`);
  }
  for (const [name, want] of Object.entries(listed)) {
    const b = files.get(name);
    if (!b) throw new Error(`manifest lists ${name}, which is missing`);
    if (typeof want?.sha256 !== "string" || sha256(b) !== want.sha256) throw new Error(`${name} does not match its manifest hash`);
  }
  return manifest;
}

export async function importBundle(svc: Service, tenant: string, files: BundleFiles): Promise<Json> {
  const manifest = verifyBundle(files);
  const text = (n: string) => (files.get(n)?.toString("utf8") ?? "").replace(/\n+$/, "");
  const out = await svc.import({ manifest: { tenant: manifest.tenant ?? tenant, tombstones: manifest.tombstones ?? [] },
    "memories.jsonl": text("memories.jsonl"), "edges.jsonl": text("edges.jsonl") }, operator(tenant));
  const { request_id: _r, ...rest } = out;
  return rest;
}

// ------------------------------------------------------------ Markdown rendering (read-only view of the records)
const fileSeg = (s: string) => (s ? s.replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 80) : "_unthreaded");

/** One Markdown file per thread (render/threads/...) and per entity (render/entities/...), records in order, each
 * with its fields as a JSON block. It is a view for people and Markdown tools; import reads memories.jsonl only. */
export function renderMarkdown(memoriesJsonl: string): Map<string, string> {
  const groups = new Map<string, Json[]>();
  for (const line of memoriesJsonl.split("\n")) {
    if (!line.trim()) continue;
    const r = JSON.parse(line);
    const key = r.kind === "entity" ? `render/entities/${fileSeg(r.meta?.name ?? r.id)}.md` : `render/threads/${fileSeg(r.thread ?? "")}.md`;
    let g = groups.get(key);
    if (!g) groups.set(key, (g = []));
    g.push(r);
  }
  const out = new Map<string, string>();
  const used = new Set<string>();
  for (const [key0, recs] of [...groups].sort(([a], [b]) => (a < b ? -1 : 1))) {
    let key = key0;
    for (let i = 2; used.has(key); i++) key = key0.replace(/\.md$/, `-${i}.md`);
    used.add(key);
    recs.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0) || String(a.observed_at).localeCompare(String(b.observed_at)) || String(a.id).localeCompare(String(b.id)));
    const thread = recs[0].kind === "entity" ? recs[0].meta?.name ?? recs[0].id : recs[0].thread ?? "";
    const lines = ["---", `thread: ${JSON.stringify(thread)}`, `records: ${recs.length}`, "---", ""];
    for (const r of recs) {
      const { content, ...fields } = r;
      lines.push(`## ${r.id}`, "", "```json", JSON.stringify(fields, null, 2), "```", "", String(content), "");
    }
    out.set(key, lines.join("\n"));
  }
  return out;
}
