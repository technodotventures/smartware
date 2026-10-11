// Authentication: the principal always comes from credentials, never from a request body (M56).
//   - API keys: a keys file holds only SHA-256 hashes of tokens (a leaked file does not leak keys).
//   - OIDC / OAuth 2.1 access tokens (JWT): verified against the issuer's JWKS; claims map to a principal.
// MCP over Streamable HTTP acts as an OAuth resource server (RFC 9728 protected-resource metadata).
import { createHash, createPublicKey, randomBytes, timingSafeEqual, verify as cverify, type KeyObject } from "node:crypto";
import { readFileSync, writeFileSync, existsSync, renameSync, statSync } from "node:fs";
import { Principal } from "@orp/core";

export class AuthError extends Error {
  constructor(message: string, readonly status = 401, readonly wwwError = "invalid_token") {
    super(message);
  }
}

export interface PrincipalSpec { id: string; groups?: string[]; tenant?: string; scopes?: string[] }

// ------------------------------------------------------------ API keys
export interface KeyEntry extends PrincipalSpec { hash: string; label?: string; created?: string }

export function hashToken(token: string): string {
  return "sha256:" + createHash("sha256").update(token).digest("hex");
}

export function newToken(): string {
  return "orp_" + randomBytes(24).toString("base64url");
}

export class KeyStore {
  private byHash = new Map<string, KeyEntry>();
  readonly warnings: string[] = [];
  private stamp = "";
  private checkedAt = 0;

  constructor(readonly path: string | null) {
    if (path && existsSync(path)) this.load(path);
  }

  /** Pick up added and revoked keys without a restart (checked at most every 2 seconds). */
  private maybeReload() {
    if (!this.path || Date.now() - this.checkedAt < 2000) return;
    this.checkedAt = Date.now();
    try {
      const st = statSync(this.path);
      const s = `${st.ino}:${st.mtimeMs}:${st.size}`;
      if (s !== this.stamp) this.load(this.path);
    } catch { /* keep the last good set */ }
  }

  private load(path: string) {
    const st = statSync(path);
    this.stamp = `${st.ino}:${st.mtimeMs}:${st.size}`;
    const next = new Map<string, KeyEntry>();
    const prev = this.byHash;
    this.byHash = next;
    try {
      this.parse(path);
    } catch (e) {
      this.byHash = prev; // a half-written or broken file never empties the key set
      throw e;
    }
  }

  private parse(path: string) {
    const raw = JSON.parse(readFileSync(path, "utf8"));
    if (Array.isArray(raw.keys)) {
      for (const k of raw.keys as KeyEntry[]) {
        if (!/^sha256:[0-9a-f]{64}$/.test(k.hash ?? "")) throw new Error(`${path}: every key needs hash "sha256:<hex>"`);
        if (!k.id) throw new Error(`${path}: every key needs an id`);
        this.byHash.set(k.hash, k);
      }
    } else {
      // legacy reference-server format {"<token>": {id, groups, tenant}}: plain tokens on disk
      for (const [tok, v] of Object.entries(raw as Record<string, PrincipalSpec>)) this.byHash.set(hashToken(tok), { ...v, hash: hashToken(tok) });
      this.warnings.push(`${path} stores plain tokens; run "orp keys migrate" to keep only hashes`);
    }
  }

  get size(): number {
    return this.byHash.size;
  }

  resolve(token: string): Principal | null {
    this.maybeReload();
    const h = hashToken(token);
    for (const [k, v] of this.byHash) {
      // constant-time comparison over the hash text
      if (k.length === h.length && timingSafeEqual(Buffer.from(k), Buffer.from(h))) return new Principal(v.id, v.groups ?? [], v.tenant ?? "default", v.scopes ?? null);
    }
    return null;
  }

  /** Add a key; returns the new token (shown once, never stored). */
  add(spec: PrincipalSpec & { label?: string }): string {
    const token = newToken();
    const e: KeyEntry = { hash: hashToken(token), id: spec.id, groups: spec.groups ?? [], tenant: spec.tenant ?? "default",
      ...(spec.scopes?.length ? { scopes: spec.scopes } : {}), label: spec.label, created: new Date().toISOString() };
    this.byHash.set(e.hash, e);
    this.save();
    return token;
  }

  revoke(id: string, label?: string): number {
    let n = 0;
    for (const [h, e] of this.byHash) if (e.id === id && (label === undefined || e.label === label)) { this.byHash.delete(h); n++; }
    this.save();
    return n;
  }

  list(): KeyEntry[] {
    return [...this.byHash.values()];
  }

  save() {
    if (!this.path) throw new Error("no keys file path");
    const tmp = this.path + ".tmp";
    writeFileSync(tmp, JSON.stringify({ keys: this.list() }, null, 1) + "\n", { mode: 0o600 });
    renameSync(tmp, this.path);
    const st = statSync(this.path);
    this.stamp = `${st.ino}:${st.mtimeMs}:${st.size}`;
  }
}

// ------------------------------------------------------------ JWT (OIDC / OAuth access tokens)
export interface OidcConfig {
  issuer: string;
  audience: string | string[];
  jwks?: string; // URL or file path; default <issuer>/.well-known/jwks.json via discovery
  principalClaim?: string; // default "sub"
  groupsClaim?: string; // default "groups"
  tenantClaim?: string; // default "tenant"; falls back to defaultTenant
  writeScopesClaim?: string; // default "orp_write_scopes": scope prefixes the token may write under
  defaultTenant?: string;
  algorithms?: string[];
  leewaySeconds?: number;
  requiredScopes?: { read?: string; write?: string };
}

interface Jwk { kid?: string; kty: string; alg?: string; use?: string; [k: string]: unknown }

const b64 = (s: string) => Buffer.from(s, "base64url");

export class JwtVerifier {
  private keys = new Map<string, KeyObject>();
  private fetchedAt = 0;
  private pending: Promise<void> | null = null;
  readonly algs: Set<string>;

  constructor(readonly cfg: OidcConfig) {
    this.algs = new Set(cfg.algorithms ?? ["RS256", "PS256", "ES256", "EdDSA"]);
  }

  private async jwksUrl(): Promise<string> {
    if (this.cfg.jwks) return this.cfg.jwks;
    const res = await fetch(this.cfg.issuer.replace(/\/$/, "") + "/.well-known/openid-configuration");
    if (!res.ok) throw new Error(`OIDC discovery failed: ${res.status}`);
    return ((await res.json()) as { jwks_uri: string }).jwks_uri;
  }

  private async refresh(force = false) {
    if (!force && Date.now() - this.fetchedAt < 10 * 60_000 && this.keys.size) return;
    if (this.pending) return this.pending;
    this.pending = (async () => {
      const src = await this.jwksUrl();
      const body = /^https?:/.test(src) ? await (await fetch(src)).json() : JSON.parse(readFileSync(src, "utf8"));
      const next = new Map<string, KeyObject>();
      for (const jwk of ((body as { keys: Jwk[] }).keys ?? [])) {
        if (jwk.use && jwk.use !== "sig") continue;
        try {
          next.set(jwk.kid ?? `_${next.size}`, createPublicKey({ key: jwk as any, format: "jwk" }));
        } catch { /* skip keys node cannot use */ }
      }
      this.keys = next;
      this.fetchedAt = Date.now();
    })().finally(() => { this.pending = null; });
    return this.pending;
  }

  async verify(token: string): Promise<{ principal: Principal; scopes: Set<string>; claims: Record<string, any> }> {
    const parts = token.split(".");
    if (parts.length !== 3) throw new AuthError("malformed token");
    let header: any, claims: any;
    try {
      header = JSON.parse(b64(parts[0]!).toString("utf8"));
      claims = JSON.parse(b64(parts[1]!).toString("utf8"));
    } catch {
      throw new AuthError("malformed token");
    }
    if (!this.algs.has(header.alg)) throw new AuthError(`algorithm ${header.alg} not allowed`);
    await this.refresh();
    let key = header.kid ? this.keys.get(header.kid) : this.keys.values().next().value;
    if (!key && header.kid) {
      await this.refresh(true); // key rotation
      key = this.keys.get(header.kid);
    }
    if (!key) throw new AuthError("unknown signing key");
    const data = Buffer.from(parts[0] + "." + parts[1]);
    const sig = b64(parts[2]!);
    const alg = header.alg as string;
    let ok = false;
    if (alg === "RS256") ok = cverify("sha256", data, key, sig);
    else if (alg === "PS256") ok = cverify("sha256", data, { key, padding: 6, saltLength: 32 }, sig); // RSA_PKCS1_PSS_PADDING
    else if (alg === "ES256") ok = cverify("sha256", data, { key, dsaEncoding: "ieee-p1363" }, sig);
    else if (alg === "EdDSA") ok = cverify(null, data, key, sig);
    if (!ok) throw new AuthError("bad signature");
    const now = Math.floor(Date.now() / 1000), leeway = this.cfg.leewaySeconds ?? 60;
    if (claims.iss !== this.cfg.issuer) throw new AuthError("wrong issuer");
    const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    const want = Array.isArray(this.cfg.audience) ? this.cfg.audience : [this.cfg.audience];
    if (!auds.some((a: string) => want.includes(a))) throw new AuthError("token not issued for this resource");
    if (typeof claims.exp !== "number" || claims.exp + leeway < now) throw new AuthError("token expired");
    if (typeof claims.nbf === "number" && claims.nbf - leeway > now) throw new AuthError("token not yet valid");
    const id = claims[this.cfg.principalClaim ?? "sub"];
    if (typeof id !== "string" || !id) throw new AuthError("token has no principal claim");
    const g = claims[this.cfg.groupsClaim ?? "groups"];
    const groups = (Array.isArray(g) ? g : typeof g === "string" ? g.split(/[\s,]+/) : []).filter((x: unknown) => typeof x === "string" && x && x !== "*");
    const tenant = claims[this.cfg.tenantClaim ?? "tenant"] ?? this.cfg.defaultTenant ?? "default";
    const scopes = new Set<string>(typeof claims.scope === "string" ? claims.scope.split(" ") : Array.isArray(claims.scp) ? claims.scp : []);
    const ws = claims[this.cfg.writeScopesClaim ?? "orp_write_scopes"];
    const writeScopes = Array.isArray(ws) ? ws.filter((x: unknown) => typeof x === "string") : null;
    return { principal: new Principal(id, groups, String(tenant), writeScopes), scopes, claims };
  }
}

/** Resolves a bearer token to a principal: API key first, then JWT when OIDC is configured. */
export class Authenticator {
  constructor(readonly keys: KeyStore, readonly jwt: JwtVerifier | null, readonly devPrincipal: Principal | null = null) {}

  async resolve(authorization: string | undefined, write: boolean): Promise<Principal> {
    if (!authorization) {
      if (this.devPrincipal) return this.devPrincipal;
      throw new AuthError("missing bearer token");
    }
    const m = /^Bearer\s+(.+)$/i.exec(authorization.trim());
    if (!m) throw new AuthError("expected a bearer token");
    const token = m[1]!.trim();
    const p = this.keys.resolve(token);
    if (p) return p;
    if (this.jwt && token.split(".").length === 3) {
      const { principal, scopes } = await this.jwt.verify(token);
      const need = write ? this.jwt.cfg.requiredScopes?.write : this.jwt.cfg.requiredScopes?.read;
      if (need && !scopes.has(need)) throw new AuthError(`token lacks scope ${need}`, 403, "insufficient_scope");
      return principal;
    }
    throw new AuthError("unknown token");
  }
}

/** Anything that turns an Authorization header into a principal (the Authenticator, or a wrapper adding groups). */
export interface PrincipalSource { resolve(authorization: string | undefined, write: boolean): Promise<Principal> }
