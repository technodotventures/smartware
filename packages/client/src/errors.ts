import type { ErrorCode } from "./types.js";

const BY_STATUS: Record<number, ErrorCode> = {
  400: "invalid_parameter", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "version_conflict",
  413: "payload_too_large", 429: "rate_limited", 503: "unavailable",
};

/** Every failure from the gateway or the network, typed. `status` is 0 when no HTTP response arrived.
 * Messages come from the gateway and never include tokens or request bodies. */
export class SmartwareError extends Error {
  override readonly name = "SmartwareError";
  constructor(
    readonly code: ErrorCode | (string & {}),
    message: string,
    readonly status = 0,
    readonly details: Record<string, unknown> = {},
    readonly requestId?: string,
  ) {
    super(message);
  }

  static fromResponse(status: number, body: unknown, requestId?: string): SmartwareError {
    const e = (body && typeof body === "object" ? (body as { error?: unknown }).error : undefined) as
      { code?: string; message?: string; details?: Record<string, unknown> } | undefined;
    if (e && typeof e.code === "string") {
      return new SmartwareError(e.code, String(e.message ?? e.code), status, e.details ?? {}, requestId);
    }
    return new SmartwareError(BY_STATUS[status] ?? "internal", `HTTP ${status}`, status, {}, requestId);
  }
}
