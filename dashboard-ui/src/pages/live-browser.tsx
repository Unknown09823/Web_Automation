import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import {
  Camera, Layers, Maximize2, Monitor, MousePointerClick, Play, RefreshCw,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useStatus, useWorkflows } from "@/stores/data-store";
import { fmtRelativeTime, statusTone } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * The Live Browser page reuses on-disk screenshots that the framework
 * writes into data/screenshots/<account_id>/. The backend doesn't have a
 * dedicated endpoint to expose the latest screenshot, so we lean on a
 * convention: pages that opt into screenshot capture write to
 *   data/screenshots/<account>/<n>.png
 * served via Nginx at /screenshots/<account>/<n>.png.
 *
 * If your nginx config doesn't expose that path the panel still renders;
 * the placeholder will explain how to wire it up.
 */


export default function LiveBrowserPage() {
  const status = useStatus();
  const recent = useWorkflows();
  const [selected, setSelected] = useState<string | null>(null);
  const [autoplay, setAutoplay] = useState(true);
  const [tick, setTick] = useState(0);

  const sessions = status?.browser_sessions ?? [];

  useEffect(() => {
    if (!selected && sessions[0]) setSelected(sessions[0]);
  }, [sessions, selected]);

  // Cache-bust the screenshot URL on a timer to simulate a stream.
  useEffect(() => {
    if (!autoplay) return;
    const id = setInterval(() => setTick((t) => t + 1), 2500);
    return () => clearInterval(id);
  }, [autoplay]);

  const lastRun = recent
    .filter((r) => r.account_id === selected)
    .slice(-1)[0];

  const screenshotUrl = selected
    ? `/screenshots/${encodeURIComponent(selected)}/latest.png?t=${tick}`
    : null;


  return (
    <div className="grid gap-5 lg:grid-cols-[280px,1fr] animate-fade-up">
      <Card className="lg:sticky lg:top-20 self-start">
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Layers className="h-3.5 w-3.5" /> Sessions
          </CardTitle>
          <Badge tone="primary">{sessions.length}</Badge>
        </CardHeader>
        <CardContent className="p-2">
          {sessions.length === 0 ? (
            <EmptyState
              icon={Monitor}
              title="No browser sessions"
              description="Run a workflow that opens a browser to see it here."
            />
          ) : (
            <ul className="space-y-1">
              {sessions.map((id) => (
                <li key={id}>
                  <button
                    onClick={() => setSelected(id)}
                    className={cn(
                      "w-full text-left px-3 py-2 rounded-md flex items-center gap-2 transition-colors text-sm",
                      selected === id
                        ? "bg-primary/15 text-foreground"
                        : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
                    )}
                  >
                    <span
                      className={cn(
                        "h-1.5 w-1.5 rounded-full shrink-0",
                        selected === id ? "bg-primary" : "bg-muted-foreground/60",
                      )}
                    />
                    <span className="font-mono truncate">{id}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>


      <div className="space-y-4">
        <Card className="overflow-hidden">
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <Camera className="h-3.5 w-3.5" />
              {selected ? selected : "No session selected"}
            </CardTitle>
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-muted-foreground">autoplay</span>
              <Switch checked={autoplay} onCheckedChange={setAutoplay} />
              <Button
                variant="outline"
                size="icon-sm"
                onClick={() => setTick((t) => t + 1)}
                aria-label="Refresh frame"
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </Button>
              {screenshotUrl && (
                <Button
                  asChild
                  variant="outline"
                  size="icon-sm"
                >
                  <a href={screenshotUrl} target="_blank" rel="noreferrer" aria-label="Open frame">
                    <Maximize2 className="h-3.5 w-3.5" />
                  </a>
                </Button>
              )}
            </div>
          </CardHeader>
          <CardContent className="p-0">
            <div className="relative aspect-[16/10] bg-[hsl(var(--background))] border-t border-border/50 overflow-hidden">
              {!selected ? (
                <Skeleton className="h-full w-full" />
              ) : (
                <>
                  <motion.img
                    key={screenshotUrl ?? ""}
                    src={screenshotUrl ?? undefined}
                    alt="latest frame"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ duration: 0.3 }}
                    className="absolute inset-0 h-full w-full object-contain bg-black/50"
                    onError={(e) => {
                      // Hide broken image so the placeholder shows through.
                      (e.currentTarget as HTMLImageElement).style.opacity = "0";
                    }}
                  />
                  <div className="absolute inset-0 grid place-items-center pointer-events-none">
                    <div className="text-center text-xs text-muted-foreground/80 max-w-md px-6">
                      <Monitor className="h-6 w-6 mx-auto mb-2 opacity-50" />
                      <div className="font-medium text-foreground/80">
                        Awaiting frame
                      </div>
                      <p className="mt-1">
                        The dashboard reads from{" "}
                        <code className="font-mono">/screenshots/{selected}/latest.png</code>.
                        Configure your workflow to capture screenshots and your reverse
                        proxy to expose the screenshots directory.
                      </p>
                    </div>
                  </div>
                  <div className="absolute top-3 left-3 flex items-center gap-2">
                    <Badge tone="primary">
                      <span className={cn("dot bg-primary live")} />
                      LIVE
                    </Badge>
                    <Badge tone="muted">tick {tick}</Badge>
                  </div>
                </>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Play className="h-3.5 w-3.5" /> Execution status
            </CardTitle>
          </CardHeader>
          <CardContent>
            {!lastRun ? (
              <EmptyState
                icon={MousePointerClick}
                title="No recent run for this session"
                description="Trigger a workflow against this account to see live execution detail."
              />
            ) : (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={statusTone(lastRun.status)}>{lastRun.status}</Badge>
                  <span className="text-sm font-medium">{lastRun.workflow}</span>
                  <span className="text-xs text-muted-foreground">
                    {fmtRelativeTime(lastRun.ended_at ?? lastRun.started_at)}
                  </span>
                </div>
                <ol className="space-y-1.5">
                  {(lastRun.records ?? []).slice(-12).map((rec, i) => {
                    const r = rec as { type?: string; name?: string; status?: string; error?: string };
                    return (
                      <li
                        key={i}
                        className="text-xs rounded-md border border-border/60 bg-secondary/30 px-2.5 py-1.5 flex items-center gap-2"
                      >
                        <span className="font-mono text-primary text-[11px]">{r.type}</span>
                        <span className="flex-1 truncate">{r.name ?? "—"}</span>
                        <Badge tone={statusTone(r.status)}>{r.status ?? "?"}</Badge>
                      </li>
                    );
                  })}
                </ol>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
