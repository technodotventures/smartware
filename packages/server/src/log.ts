// Structured logs (JSON lines on stderr). Request bodies, memory content and tokens are never logged.
export class Log {
  constructor(readonly level: "debug" | "info" | "warn" | "error" | "silent" = "info") {}
  private rank = { debug: 10, info: 20, warn: 30, error: 40, silent: 99 };
  private emit(level: "debug" | "info" | "warn" | "error", msg: string, f: Record<string, unknown>) {
    if (this.rank[level] < this.rank[this.level]) return;
    process.stderr.write(JSON.stringify({ ts: new Date().toISOString(), level, msg, ...f }) + "\n");
  }
  debug(m: string, f: Record<string, unknown> = {}) { this.emit("debug", m, f); }
  info(m: string, f: Record<string, unknown> = {}) { this.emit("info", m, f); }
  warn(m: string, f: Record<string, unknown> = {}) { this.emit("warn", m, f); }
  error(m: string, f: Record<string, unknown> = {}) { this.emit("error", m, f); }
}
