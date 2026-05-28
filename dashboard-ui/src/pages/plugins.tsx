import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import {
  Boxes, CheckCircle2, Cog, RefreshCcw, Search, ShieldAlert,
} from "lucide-react";
import { toast } from "sonner";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import { api } from "@/lib/api";
import { useDataStore, usePlugins } from "@/stores/data-store";
import { cn } from "@/lib/utils";
import type { PluginRecord } from "@/types/api";


export default function PluginsPage() {
  const plugins = usePlugins();
  const refresh = useDataStore((s) => s.refreshPlugins);
  const [query, setQuery] = useState("");
  const [pending, setPending] = useState<string | null>(null);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return plugins;
    return plugins.filter((p) =>
      [p.name, p.module_path].filter(Boolean).join(" ").toLowerCase().includes(q),
    );
  }, [plugins, query]);

  async function toggle(p: PluginRecord) {
    setPending(p.name);
    try {
      const t = toast.loading(`${p.enabled ? "disabling" : "enabling"} ${p.name}…`);
      if (p.enabled) await api.plugins.disable(p.name);
      else await api.plugins.enable(p.name);
      toast.success(`${p.name} ${p.enabled ? "disabled" : "enabled"}`, { id: t });
      await refresh();
    } catch (e) {
      toast.error(`toggle failed: ${e instanceof Error ? e.message : "unknown"}`);
    } finally {
      setPending(null);
    }
  }

  async function restartOne(p: PluginRecord) {
    setPending(p.name);
    try {
      const t = toast.loading(`restarting ${p.name}…`);
      await api.plugins.restart(p.name);
      toast.success(`${p.name} restarted`, { id: t });
      await refresh();
    } catch (e) {
      toast.error(`restart failed: ${e instanceof Error ? e.message : "unknown"}`);
    } finally {
      setPending(null);
    }
  }

  async function reloadAll() {
    try {
      const t = toast.loading("reloading all plugins…");
      await api.plugins.reloadAll();
      toast.success("plugins reloaded", { id: t });
      await refresh();
    } catch (e) {
      toast.error(`reload failed: ${e instanceof Error ? e.message : "unknown"}`);
    }
  }


  return (
    <div className="space-y-5 animate-fade-up">
      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2">
          <CardTitle>Plugin marketplace</CardTitle>
          <div className="flex items-center gap-2">
            <div className="relative w-64 max-w-full">
              <Search className="h-3.5 w-3.5 absolute left-3 top-3 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search plugins…"
                className="pl-9"
              />
            </div>
            <Button variant="outline" size="sm" onClick={reloadAll}>
              <RefreshCcw className="h-3.5 w-3.5" /> Reload all
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {plugins.length === 0 ? (
            <Skeleton className="h-32 w-full" />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={Boxes}
              title="No plugins match"
              description="Drop modules into plugins_external/ and reload."
            />
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {filtered.map((p) => (
                <PluginCard
                  key={p.name}
                  p={p}
                  onToggle={() => toggle(p)}
                  onRestart={() => restartOne(p)}
                  busy={pending === p.name}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}


function PluginCard({
  p, onToggle, onRestart, busy,
}: {
  p: PluginRecord;
  onToggle: () => void;
  onRestart: () => void;
  busy: boolean;
}) {
  const errored = !!p.error;
  const healthy = p.enabled && p.started && !errored;
  return (
    <motion.div
      whileHover={{ y: -2 }}
      className={cn(
        "panel p-4 flex flex-col gap-3 relative overflow-hidden",
        errored && "border-destructive/40",
      )}
    >
      <div
        className={cn(
          "absolute -right-12 -top-12 h-32 w-32 rounded-full blur-3xl opacity-40",
          healthy ? "bg-success/20" : errored ? "bg-destructive/30" : "bg-muted-foreground/20",
        )}
        aria-hidden
      />
      <div className="relative flex items-start gap-3">
        <span className="grid place-items-center h-10 w-10 rounded-lg border border-border/60 bg-background/60">
          <Boxes className="h-4 w-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium truncate">{p.name}</span>
            {p.metadata?.version != null && (
              <Badge tone="muted">v{String(p.metadata.version)}</Badge>
            )}
          </div>
          <div className="text-[11px] text-muted-foreground font-mono truncate">
            {p.module_path ?? "—"}
          </div>
        </div>
        <Switch
          checked={p.enabled}
          onCheckedChange={onToggle}
          aria-label={`toggle ${p.name}`}
          disabled={busy}
        />
      </div>

      <div className="relative flex items-center gap-2 text-xs">
        {errored ? (
          <Badge tone="destructive">
            <ShieldAlert className="h-3 w-3" /> error
          </Badge>
        ) : healthy ? (
          <Badge tone="success">
            <CheckCircle2 className="h-3 w-3" /> healthy
          </Badge>
        ) : p.enabled ? (
          <Badge tone="warning">starting</Badge>
        ) : (
          <Badge tone="muted">disabled</Badge>
        )}
        <span className="text-muted-foreground">
          {p.started ? "started" : "stopped"}
        </span>
      </div>

      {errored && (
        <pre className="relative text-[11px] bg-destructive/10 text-destructive rounded-md p-2 max-h-24 overflow-y-auto whitespace-pre-wrap">
          {p.error}
        </pre>
      )}

      <div className="relative mt-auto flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          className="flex-1"
          onClick={onRestart}
          disabled={busy}
        >
          <RefreshCcw className="h-3.5 w-3.5" /> Restart
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label="settings">
          <Cog className="h-3.5 w-3.5" />
        </Button>
      </div>
    </motion.div>
  );
}
