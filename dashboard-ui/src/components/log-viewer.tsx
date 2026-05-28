import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Pause, Play, Search, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { fmtTime } from "@/lib/format";

interface LogLine {
  raw: string;
  level: "error" | "warn" | "info" | "debug" | "other";
}

const LEVEL_RE = /\b(ERROR|WARN(ING)?|INFO|DEBUG)\b/i;
const TS_RE = /^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/;

function classify(raw: string): LogLine {
  const m = LEVEL_RE.exec(raw);
  let level: LogLine["level"] = "other";
  if (m) {
    const u = m[1].toUpperCase();
    if (u === "ERROR") level = "error";
    else if (u.startsWith("WARN")) level = "warn";
    else if (u === "INFO") level = "info";
    else if (u === "DEBUG") level = "debug";
  }
  return { raw, level };
}


export interface LogViewerProps {
  lines: string[] | null;
  loading?: boolean;
  toolbar?: React.ReactNode;
  height?: number;
  /** Auto-scroll to the bottom when new lines arrive (default: true). */
  follow?: boolean;
}

const LEVEL_STYLES: Record<LogLine["level"], string> = {
  error: "text-destructive",
  warn: "text-warning",
  info: "text-foreground/90",
  debug: "text-muted-foreground",
  other: "text-foreground/80",
};

const LEVEL_BAR: Record<LogLine["level"], string> = {
  error: "bg-destructive",
  warn: "bg-warning",
  info: "bg-primary/60",
  debug: "bg-muted-foreground/40",
  other: "bg-border",
};

export function LogViewer({
  lines,
  loading = false,
  toolbar,
  height = 420,
  follow = true,
}: LogViewerProps) {
  const [filter, setFilter] = useState("");
  const [levels, setLevels] = useState<Record<LogLine["level"], boolean>>({
    error: true,
    warn: true,
    info: true,
    debug: true,
    other: true,
  });
  const [paused, setPaused] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  const parsed = useMemo<LogLine[]>(
    () => (lines ?? []).map(classify),
    [lines],
  );

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return parsed.filter((l) => {
      if (!levels[l.level]) return false;
      if (q && !l.raw.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [parsed, filter, levels]);

  useEffect(() => {
    if (!follow || paused) return;
    const el = ref.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [filtered, follow, paused]);


  const counts = useMemo(() => {
    const c: Record<LogLine["level"], number> = {
      error: 0, warn: 0, info: 0, debug: 0, other: 0,
    };
    for (const l of parsed) c[l.level] += 1;
    return c;
  }, [parsed]);

  return (
    <div className="panel overflow-hidden flex flex-col">
      <div className="px-4 py-3 border-b border-border/60 flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[180px]">
          <Search className="h-3.5 w-3.5 absolute left-2.5 top-2.5 text-muted-foreground" />
          <Input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter…"
            className="pl-8 h-8"
          />
        </div>
        {(["error", "warn", "info", "debug"] as const).map((l) => (
          <button
            key={l}
            onClick={() => setLevels((s) => ({ ...s, [l]: !s[l] }))}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md border px-2 h-7 text-[11px] uppercase tracking-wider transition-colors",
              levels[l]
                ? "border-border/70 bg-secondary/60 text-foreground"
                : "border-dashed border-border/40 text-muted-foreground/60",
            )}
          >
            <span className={cn("h-1.5 w-1.5 rounded-full", LEVEL_BAR[l])} />
            {l} <span className="opacity-60 tabular-nums">{counts[l]}</span>
          </button>
        ))}
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setPaused((p) => !p)}
          aria-label={paused ? "Resume autoscroll" : "Pause autoscroll"}
        >
          {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
          {paused ? "Resume" : "Pause"}
        </Button>
        {toolbar}
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => {
            const el = ref.current;
            if (el) el.scrollTop = el.scrollHeight;
          }}
          aria-label="Scroll to bottom"
        >
          <ChevronDown className="h-3.5 w-3.5" />
        </Button>
      </div>
      <div
        ref={ref}
        style={{ height }}
        className="bg-[hsl(var(--background))]/60 font-mono text-[12px] leading-relaxed overflow-y-auto"
      >
        {loading ? (
          <div className="p-4 space-y-2">
            {Array.from({ length: 12 }).map((_, i) => (
              <Skeleton key={i} className="h-3 w-full" />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <div className="p-6 text-center text-xs text-muted-foreground">
            <Trash2 className="h-4 w-4 mx-auto mb-2 opacity-60" />
            No log lines match the current filter.
          </div>
        ) : (
          <div className="py-1">
            {filtered.map((l, i) => (
              <div
                key={i}
                className={cn(
                  "flex items-start gap-2 px-3 py-0.5 hover:bg-secondary/40",
                  LEVEL_STYLES[l.level],
                )}
              >
                <span
                  className={cn("mt-1 h-3 w-0.5 rounded-full shrink-0", LEVEL_BAR[l.level])}
                />
                <span className="break-all whitespace-pre-wrap">{l.raw}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <div className="px-4 py-2 border-t border-border/60 text-[11px] text-muted-foreground flex items-center justify-between">
        <span>
          {filtered.length} / {parsed.length} lines
        </span>
        <span className="font-mono">{fmtTime(Date.now() / 1000)}</span>
      </div>
    </div>
  );
}
