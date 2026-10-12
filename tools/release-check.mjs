// Release gate (run in CI and before any publish). Fails on:
//   - npm lifecycle scripts in any package (preinstall, install, postinstall, prepare, prepublish, prepublishOnly,
//     preuninstall, uninstall, postuninstall): the July 2026 compromise ran through exactly this path
//   - dependencies outside the allowlist (only @technodotventures/smartware-* at runtime; TypeScript and Node types for development)
//   - published files outside each package's "files" allowlist, or anything that looks like state or secrets
//   - payload signatures from the 2026 incident (atob(, eval("global., global.<x>='<digits>-, the createRequire
//     prepend followed by an over-long line) and any over-long line in source
//   - credentials in tracked files
// Writes dist-release/sbom.cdx.json (CycloneDX) and dist-release/SHA256SUMS for the files each package would publish.
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("..", import.meta.url).pathname.replace(/\/$/, "");
const fails = [];
const fail = (m) => fails.push(m);
const LIFECYCLE = ["preinstall", "install", "postinstall", "prepare", "prepublish", "prepublishOnly", "prepack", "postpack",
  "preuninstall", "uninstall", "postuninstall"];
const DEV_ALLOW = new Set(["typescript", "@types/node"]);

const pkgDirs = ["packages/core", "packages/server", "packages/client", "packages/connectors",
  ...readdirSync(join(ROOT, "packages/adapters")).filter((d) => existsSync(join(ROOT, "packages/adapters", d, "package.json"))).map((d) => `packages/adapters/${d}`)];

// ---- packages
const root = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8"));
for (const k of Object.keys(root.scripts ?? {})) if (LIFECYCLE.includes(k)) fail(`root package.json: lifecycle script "${k}"`);
for (const d of Object.keys({ ...root.dependencies })) fail(`root package.json: runtime dependency ${d}`);
for (const d of Object.keys({ ...root.devDependencies })) if (!DEV_ALLOW.has(d)) fail(`root package.json: dev dependency ${d} not in the allowlist`);
const components = [];
for (const dir of pkgDirs) {
  const p = JSON.parse(readFileSync(join(ROOT, dir, "package.json"), "utf8"));
  for (const k of Object.keys(p.scripts ?? {})) if (LIFECYCLE.includes(k)) fail(`${dir}: lifecycle script "${k}"`);
  for (const [d] of Object.entries({ ...p.dependencies, ...p.optionalDependencies })) if (!(d === "smartware" || d.startsWith("@technodotventures/smartware-"))) fail(`${dir}: runtime dependency ${d} outside the allowlist`);
  for (const d of Object.keys(p.devDependencies ?? {})) if (!DEV_ALLOW.has(d)) fail(`${dir}: dev dependency ${d} not in the allowlist`);
  if (!Array.isArray(p.files)) fail(`${dir}: no "files" allowlist`);
  if (p.license !== "Apache-2.0") fail(`${dir}: licence is ${p.license}`);
  if (p.repository?.url !== "git+https://github.com/technodotventures/smartware.git" || p.repository?.directory !== dir) fail(`${dir}: repository must point at technodotventures/smartware with directory "${dir}" (npm provenance checks it)`);
  // the files npm would publish: package.json, README, LICENSE, plus the "files" allowlist
  const files = [];
  const walk = (rel) => {
    const abs = join(ROOT, dir, rel);
    if (!existsSync(abs)) return;
    if (statSync(abs).isDirectory()) for (const f of readdirSync(abs)) walk(join(rel, f));
    else files.push(rel);
  };
  for (const f of ["package.json", "README.md", "LICENSE", ...(p.files ?? [])]) walk(f);
  for (const f of files) {
    if (/(^|\/)(\.env|keys\.json|.*\.db|.*\.db-wal|.*\.pem|.*\.key|id_rsa.*|.*\.swenc|.*\.pkl)$/.test(f)) fail(`${dir}: would publish ${f}`);
    if (/(^|\/)test(s)?\//.test(f)) fail(`${dir}: would publish test file ${f}`);
  }
  const hashes = files.sort().map((f) => ({ f, sha: createHash("sha256").update(readFileSync(join(ROOT, dir, f))).digest("hex") }));
  components.push({ name: p.name, version: p.version, dir, files: hashes, deps: Object.keys(p.dependencies ?? {}) });
}

// ---- payload signatures and over-long lines in source (tracked files)
const tracked = execFileSync("git", ["ls-files"], { cwd: ROOT, encoding: "utf8" }).split("\n").filter(Boolean);
const SRC = /\.(m?js|cjs|ts|py|sh)$/;
const SIGS = [[/atob\(/, "atob("], [/eval\(\s*["']global\./, 'eval("global.'], [/global\.[a-zA-Z_$]+\s*=\s*['"]\d+-/, "global.<x>='<digits>-"],
  [/global\[['"][^'"]+['"]\]\s*=\s*['"]\d+-\d+/, "global['x']='<id>'"], [/_\$_[0-9a-f]{3,}/, "_$_ marker"], [/eth_getLogs|cloudflare-eth/, "EtherHiding C2 lookup"]];
const ALLOW_SIG = new Set(["tools/release-check.mjs", "tools/hooks/pre-push"]);
for (const f of tracked) {
  if (!SRC.test(f) || ALLOW_SIG.has(f)) continue;
  const text = readFileSync(join(ROOT, f), "utf8");
  const lines = text.split("\n");
  lines.forEach((l, i) => { if (l.length > 1000) fail(`${f}:${i + 1}: line of ${l.length} characters (payloads hide in long lines)`); });
  for (const [rx, name] of SIGS) if (rx.test(text)) fail(`${f}: payload signature ${name}`);
  if (/createRequire\(import\.meta\.url\)/.test(lines.slice(0, 3).join("\n")) && lines.some((l) => l.length > 500)) fail(`${f}: createRequire prepend with a long line`);
  if (/\s{200,}\S/.test(text)) fail(`${f}: a run of 200+ whitespace characters before code (hidden payload pattern)`);
}

// ---- credentials in tracked files
const SECRET = [[/-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----/, "private key"], [/\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/, "AWS key"],
  [/\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b/, "GitHub token"], [/\bsk-ant-[A-Za-z0-9_-]{30,}\b/, "Anthropic key"],
  [/\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b/, "OpenAI key"], [/\bxox[abprs]-[0-9A-Za-z-]{20,}\b/, "Slack token"], [/\bnpm_[A-Za-z0-9]{36}\b/, "npm token"]];
const SECRET_ALLOW = /(^|\/)test(s)?\/|secrets\.ts$|release-check\.mjs$|test_.*\.py$/;
for (const f of tracked) {
  if (SECRET_ALLOW.test(f) || /\.(png|jpg|gz|db|swenc|pkl)$/.test(f)) continue;
  let text;
  try { text = readFileSync(join(ROOT, f), "utf8"); } catch { continue; }
  for (const [rx, name] of SECRET) if (rx.test(text)) fail(`${f}: looks like a ${name}`);
}

// ---- SBOM and checksums
mkdirSync(join(ROOT, "dist-release"), { recursive: true });
const sbom = { bomFormat: "CycloneDX", specVersion: "1.5", version: 1, metadata: { component: { type: "application", name: root.name, version: root.version } },
  components: components.map((c) => ({ type: "library", name: c.name, version: c.version, purl: `pkg:npm/${c.name.replace("@", "%40")}@${c.version}`,
    licenses: [{ license: { id: "Apache-2.0" } }], hashes: [{ alg: "SHA-256", content: createHash("sha256").update(c.files.map((x) => x.sha + "  " + x.f).join("\n")).digest("hex") }] })),
  dependencies: components.map((c) => ({ ref: c.name, dependsOn: c.deps })) };
writeFileSync(join(ROOT, "dist-release", "sbom.cdx.json"), JSON.stringify(sbom, null, 1) + "\n");
writeFileSync(join(ROOT, "dist-release", "SHA256SUMS"), components.flatMap((c) => c.files.map((x) => `${x.sha}  ${relative(ROOT, join(ROOT, c.dir, x.f))}`)).join("\n") + "\n");

if (fails.length) {
  console.error("release check FAILED:\n  " + fails.join("\n  "));
  process.exit(1);
}
console.log(`release check passed: ${components.length} packages, no lifecycle scripts, no third-party runtime dependencies, no payload signatures, no credentials; SBOM and checksums in dist-release/`);
