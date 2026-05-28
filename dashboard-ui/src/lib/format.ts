/**
 * Display formatters used across the dashboard.
 * Keep these stable — they're consumed by ALL pages.
 */

export function fmtNumber(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

export function fmtPercent(
  n: number | null | undefined,
  digits = 1,
  alreadyPercent = true,
): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const v = alreadyPercent ? n : n * 100;
  return `${v.toFixed(digits)}%`;
}

export function fmtBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let n = bytes / 1024;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return `${m}m ${r}s`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  if (h < 24) return `${h}h ${mm}m`;
  const d = Math.floor(h / 24);
  const hh = h % 24;
  return `${d}d ${hh}h`;
}

export function fmtRelativeTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  // ts may arrive as either seconds-since-epoch or ms — detect by magnitude.
  const ms = ts < 1e12 ? ts * 1000 : ts;
  const diff = Date.now() - ms;
  const sec = Math.round(diff / 1000);
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const m = Math.round(sec / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

export function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  const ms = ts < 1e12 ? ts * 1000 : ts;
  return new Date(ms).toLocaleTimeString();
}

export function fmtDateTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  const ms = ts < 1e12 ? ts * 1000 : ts;
  return new Date(ms).toLocaleString();
}

/**
 * Color-code a status string against the dashboard palette.
 * Used by badges, pills, dots throughout the app.
 */
export function statusTone(
  s: string | undefined | null,
):
  | "success"
  | "warning"
  | "destructive"
  | "primary"
  | "muted" {
  if (!s) return "muted";
  const v = s.toLowerCase();
  if (v.includes("succ") || v === "completed" || v === "ok" || v === "ready")
    return "success";
  if (
    v.includes("fail") ||
    v.includes("error") ||
    v === "rejected" ||
    v === "stopped"
  )
    return "destructive";
  if (
    v.includes("running") ||
    v.includes("starting") ||
    v.includes("restart") ||
    v.includes("active")
  )
    return "primary";
  if (
    v.includes("pending") ||
    v.includes("paused") ||
    v.includes("warn") ||
    v.includes("queued")
  )
    return "warning";
  return "muted";
}
