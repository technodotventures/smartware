// Markdown vault connector (Obsidian, Basic Memory and Sylph-style repos) and the reverse renderer.
//
// Ingest: every .md file under the vault is a `document`, or (by default) one record per "## " section, all in one
// thread named after the file path. Readers come from the file's frontmatter `readers:`, else the nearest
// `.orp-acl.json` folder file ({"readers": [...]}), else the connector's configured default. Moves (same content at a
// new path) keep their record ids; deletions are forgotten.
//
// Render: renderVault writes the bundle's Markdown rendering (spec §8.3 render/): one file per thread, frontmatter
// carrying the record fields, so a vault can be rebuilt from ORP. "Two-way" is at the file level: see the README.
import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { OrpHttp, type Json } from "./http.js";
import { formatFrontmatter, parseNote, type FrontValue, type Frontmatter } from "./frontmatter.js";
import { defineConnector, type Connector, type SourceItem } from "./sdk.js";

export interface VaultOptions {
  /** Vault root directory. */
  dir: string;
  /** Readers when neither frontmatter nor a .orp-acl.json says otherwise. Required, so nothing defaults to the tenant. */
  defaultReaders: string[];
  name?: string; // default "vault"
  scopePrefix?: string; // default "vault"
  /** "sections" (default): one record per "## " section; "file": one record per file. */
  split?: "sections" | "file";
  /** Put each folder in its own scope below the prefix (default true). */
  scopeByFolder?: boolean;
  principalResolver?: (sourcePrincipal: string) => string | null;
  /** Folder names to skip (default .git, .obsidian, .trash, node_modules). Dot folders are always skipped. */
  ignore?: string[];
  /** Files above this size are skipped (default 1 MiB). */
  maxFileBytes?: number;
}

const ACL_FILE = ".orp-acl.json";
/** Frontmatter keys that renderVault writes from record fields; on ingest they are not copied into meta. */
const RECORD_KEYS = new Set(["readers", "scope", "kind"]);
const MARKER = /^<!-- orp \{.*\} -->[ \t]*(\r?\n|$)/gm;
const sha = (s: string) => createHash("sha256").update(s).digest("hex");

export function slug(s: string): string {
  const x = s.toLowerCase().normalize("NFKD").replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-+|-+$/g, "");
  return x.slice(0, 64) || "section";
}

export interface Section { key: string; content: string }

/** Split a note body into its preamble and "## " sections. Each section keeps its heading line. ORP's own record
 * markers (written by renderVault) are removed first, so a rendered vault re-ingests to the same records. */
export function splitSections(body: string): Section[] {
  const text = body.replace(MARKER, "");
  const lines = text.split(/\r?\n/);
  const chunks: { heading: string | null; lines: string[] }[] = [{ heading: null, lines: [] }];
  let fence = false;
  for (const line of lines) {
    if (/^(```|~~~)/.test(line)) fence = !fence;
    if (!fence && /^## /.test(line)) chunks.push({ heading: line.slice(3).trim(), lines: [line] });
    else chunks[chunks.length - 1]!.lines.push(line);
  }
  const seen = new Map<string, number>();
  const out: Section[] = [];
  for (const c of chunks) {
    const content = c.lines.join("\n").replace(/^\s*\n/, "").trimEnd();
    if (!content.trim()) continue;
    let key = c.heading === null ? "_" : slug(c.heading);
    const n = (seen.get(key) ?? 0) + 1;
    seen.set(key, n);
    if (n > 1) key = `${key}-${n}`;
    out.push({ key, content });
  }
  return out;
}

function readersFrom(v: FrontValue | undefined): string[] | null {
  if (v === undefined || v === null) return null;
  if (Array.isArray(v)) return v.map(String);
  if (typeof v === "string") return v.split(",").map((x) => x.trim()).filter(Boolean);
  return null;
}

function scopeSegment(s: string): string {
  const x = s.replace(/~/g, "-").replace(/\s+/g, " ").trim();
  return x && x !== "." && x !== ".." ? x : "_";
}

interface FileCursor { hash: string; ids: string[] }
interface VaultCursor { files: Record<string, FileCursor> }

export function vaultConnector(opt: VaultOptions): Connector {
  if (!opt.dir) throw new Error("vault dir is required");
  if (!Array.isArray(opt.defaultReaders)) throw new Error("defaultReaders is required (use [\"public\"] to share with the whole tenant)");
  const root = resolve(opt.dir);
  const prefix = (opt.scopePrefix ?? "vault").replace(/\/+$/, "");
  const ignore = new Set(opt.ignore ?? [".git", ".obsidian", ".trash", "node_modules"]);
  const maxBytes = opt.maxFileBytes ?? 1 << 20;
  const split = opt.split ?? "sections";

  const aclCache = new Map<string, { readers: string[] | null; mtime: number }>();
  const folderAcl = (relDir: string): { readers: string[] | null; mtime: number } => {
    const hit = aclCache.get(relDir);
    if (hit) return hit;
    const p = join(root, relDir, ACL_FILE);
    let out: { readers: string[] | null; mtime: number };
    if (existsSync(p) && lstatSync(p).isFile()) {
      let parsed: any;
      try { parsed = JSON.parse(readFileSync(p, "utf8")); } catch { throw new Error(`${join(relDir, ACL_FILE)} is not valid JSON; fix it before syncing (failing closed)`); }
      if (!parsed || !Array.isArray(parsed.readers) || !parsed.readers.every((x: unknown) => typeof x === "string")) {
        throw new Error(`${join(relDir, ACL_FILE)} must be {"readers": ["..."]} (failing closed)`);
      }
      out = { readers: parsed.readers, mtime: lstatSync(p).mtimeMs };
    } else if (relDir === "" || relDir === ".") {
      out = { readers: null, mtime: 0 };
    } else {
      const parent = relDir.split(sep).slice(0, -1).join(sep);
      out = folderAcl(parent);
    }
    aclCache.set(relDir, out);
    return out;
  };

  const walk = (relDir: string, files: string[]) => {
    for (const ent of readdirSync(join(root, relDir), { withFileTypes: true })) {
      if (ent.name.startsWith(".") || ignore.has(ent.name)) continue;
      const rel = relDir ? join(relDir, ent.name) : ent.name;
      if (ent.isSymbolicLink()) continue; // never follow links out of the vault
      if (ent.isDirectory()) walk(rel, files);
      else if (ent.isFile() && ent.name.toLowerCase().endsWith(".md")) files.push(rel);
    }
  };

  return defineConnector({
    name: opt.name ?? "vault",
    scopePrefix: prefix,
    principalResolver: opt.principalResolver,
    pull(cursor) {
      aclCache.clear();
      const prev: VaultCursor = cursor && typeof cursor === "object" && (cursor as any).files ? cursor as VaultCursor : { files: {} };
      const files: string[] = [];
      walk("", files);
      files.sort();
      const items: SourceItem[] = [];
      const next: VaultCursor = { files: {} };
      const byHash = new Map<string, string>(); // body hash -> previous path no longer present (move detection)
      const present = new Set(files.map((f) => f.split(sep).join("/")));
      for (const [p, fc] of Object.entries(prev.files)) if (!present.has(p)) byHash.set(fc.hash, p);
      for (const rel of files) {
        const st = lstatSync(join(root, rel));
        if (st.size > maxBytes) continue;
        const path = rel.split(sep).join("/");
        const raw = readFileSync(join(root, rel), "utf8");
        const { frontmatter, body } = parseNote(raw);
        const relDir = rel.split(sep).slice(0, -1).join(sep);
        const folder = folderAcl(relDir);
        const acl = readersFrom(frontmatter.readers) ?? folder.readers ?? opt.defaultReaders;
        const fm: Frontmatter = {};
        for (const [k, v] of Object.entries(frontmatter)) if (!RECORD_KEYS.has(k) && !k.startsWith("orp_")) fm[k] = v;
        const sections = split === "file" ? [{ key: "", content: body.replace(MARKER, "").trim() }].filter((s) => s.content) : splitSections(body);
        const hash = sha(body.replace(MARKER, ""));
        const movedFrom = prev.files[path] ? undefined : byHash.get(hash);
        if (movedFrom) byHash.delete(hash);
        const dirSegs = path.split("/").slice(0, -1).map(scopeSegment);
        const scope = opt.scopeByFolder === false || !dirSegs.length ? prefix : `${prefix}/${dirSegs.join("/")}`;
        const changedAt = Math.max(st.mtimeMs, folder.mtime);
        const eventTime = typeof fm.date === "string" && /^\d{4}-\d{2}-\d{2}/.test(fm.date) ? fm.date : undefined;
        const ids: string[] = [];
        sections.forEach((s, i) => {
          const id = s.key ? `${path}#${s.key}` : path;
          ids.push(id);
          items.push({
            id, content: s.content, acl, kind: "document", scope, thread: path, seq: i,
            uri: `file://${encodeURI(path)}${s.key ? "#" + encodeURIComponent(s.key) : ""}`,
            changedAt, ...(eventTime ? { eventTime } : {}),
            ...(movedFrom ? { movedFrom: s.key ? `${movedFrom}#${s.key}` : movedFrom } : {}),
            meta: { "x-vault": { path, section: s.key || null, ...(Object.keys(fm).length ? { frontmatter: fm } : {}) } },
          });
        });
        next.files[path] = { hash, ids };
      }
      const now = new Set(items.map((i) => i.id));
      const moved = new Set(items.map((i) => i.movedFrom).filter(Boolean));
      const deletions: string[] = [];
      for (const fc of Object.values(prev.files)) for (const id of fc.ids) if (!now.has(id) && !moved.has(id)) deletions.push(id);
      return { items, deletions, cursor: next };
    },
  });
}

// ------------------------------------------------------------ render back

export interface RenderOptions { tenant?: string; /** Include records with these statuses (default ["active"]). */ statuses?: string[] }
export interface RenderResult { files: string[]; records: number }

/** A thread name is used as a file path only when it is a plain relative .md path; anything else is slugged. */
function safeRelPath(thread: string): string | null {
  if (!/\.md$/i.test(thread) || thread.length > 240) return null;
  const segs = thread.split("/");
  if (segs.some((s) => !s || s === "." || s === ".." || s.startsWith(".") || /[\\\0:]/.test(s))) return null;
  return segs.join("/");
}

const markerJson = (o: Json) => JSON.stringify(o).replace(/</g, "\\u003c").replace(/>/g, "\\u003e");

function uniform<T>(xs: T[]): T | undefined {
  const k = JSON.stringify(xs[0]);
  return xs.every((x) => JSON.stringify(x) === k) ? xs[0] : undefined;
}

/** Write the Markdown rendering of everything the token's principal can read: one file per thread of evidence
 * (documents and events; records without a thread get one file each), frontmatter = record fields, and one marker
 * comment per record. Derived records (summaries, facts, observations) go under .orp/derived/, a dot folder the vault
 * connector never ingests, so re-ingesting a rendered vault does not turn derived memories into evidence. */
export async function renderVault(url: string, token: string, outDir: string, opt: RenderOptions = {}): Promise<RenderResult> {
  const http = new OrpHttp({ url, token, tenant: opt.tenant });
  const bundle = await http.export();
  const statuses = new Set(opt.statuses ?? ["active"]);
  const recs: Json[] = String(bundle["memories.jsonl"] ?? "").split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l))
    .filter((r) => statuses.has(r.status));
  const groups = new Map<string, Json[]>();
  for (const r of recs) {
    const evidence = r.kind === "document" || r.kind === "event";
    const k = !evidence ? `d:${r.thread || r.id}` : r.thread ? `t:${r.thread}` : `r:${r.id}`;
    let a = groups.get(k);
    if (!a) groups.set(k, (a = []));
    a.push(r);
  }
  const root = resolve(outDir);
  mkdirSync(root, { recursive: true });
  const used = new Set<string>();
  const files: string[] = [];
  for (const [k, rs] of [...groups].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    rs.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0) || String(a.observed_at).localeCompare(String(b.observed_at)) || String(a.id).localeCompare(String(b.id)));
    const thread = k.startsWith("t:") ? k.slice(2) : null;
    let rel = k.startsWith("d:") ? `.orp/derived/${slug(k.slice(2))}.md`
      : thread ? safeRelPath(thread) ?? `threads/${slug(thread)}.md` : `records/${slug(String(rs[0]!.id))}.md`;
    for (let n = 2; used.has(rel.toLowerCase()); n++) rel = rel.replace(/(-\d+)?\.md$/i, `-${n}.md`);
    used.add(rel.toLowerCase());
    const target = resolve(root, rel);
    if (!target.startsWith(root + sep)) throw new Error("refusing to write outside the output directory");
    const fm: Frontmatter = {};
    const orig = rs[0]!.meta?.["x-vault"]?.frontmatter;
    if (orig && typeof orig === "object") {
      for (const [kk, v] of Object.entries(orig as Json)) {
        if (v === null || ["string", "number", "boolean"].includes(typeof v) || (Array.isArray(v) && v.every((x) => typeof x === "string"))) fm[kk] = v as FrontValue;
      }
    }
    if (thread) fm.orp_thread = thread;
    else if (k.startsWith("d:") && rs[0]!.thread) fm.orp_thread = rs[0]!.thread;
    fm.orp_ids = rs.map((r) => String(r.id));
    fm.orp_versions = rs.map((r) => String(r.version));
    const sc = uniform(rs.map((r) => r.scope ?? ""));
    if (sc) fm.scope = sc;
    const kind = uniform(rs.map((r) => r.kind));
    if (kind) fm.kind = kind;
    const readers = uniform(rs.map((r) => (r.readers === null ? null : [...r.readers].sort())));
    if (readers !== undefined) fm.readers = readers === null ? ["public"] : readers;
    else fm.orp_readers = "mixed";
    const parts = rs.map((r) => {
      const mk = { id: r.id, version: r.version, kind: r.kind, status: r.status, readers: r.readers, actor: r.actor || undefined,
        origin: r.origin, observed_at: r.observed_at, event_time: r.event_time ?? undefined,
        sources: (r.sources ?? []).map((s: Json) => s.uri), derived_from: r.derived_from?.length ? r.derived_from : undefined };
      return `<!-- orp ${markerJson(mk)} -->\n${String(r.content).trimEnd()}`;
    });
    mkdirSync(resolve(target, ".."), { recursive: true });
    if (existsSync(target) && lstatSync(target).isSymbolicLink()) throw new Error(`refusing to write through a symlink: ${rel}`);
    writeFileSync(target, formatFrontmatter(fm) + "\n" + parts.join("\n\n") + "\n", { mode: 0o600 });
    files.push(relative(root, target).split(sep).join("/"));
  }
  return { files, records: recs.length };
}
