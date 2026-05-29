import { useEffect, useMemo, useState } from "react";
import {
  ChevronRight, Code2, FileCode2, ListTree, Play, Repeat, Workflow as WorkflowIcon,
} from "lucide-react";
import { motion } from "framer-motion";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { useDataStore, useWorkflows } from "@/stores/data-store";
import { fmtMs, fmtRelativeTime, statusTone } from "@/lib/format";
import { cn } from "@/lib/utils";


interface WorkflowDef {
  name?: string;
  description?: string;
  inputs?: Record<string, unknown>;
  steps?: Array<Record<string, unknown>>;
}

export default function WorkflowsPage() {
  const recent = useWorkflows();
  const refresh = useDataStore((s) => s.fetchAll);
  const [files, setFiles] = useState<string[] | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [def, setDef] = useState<WorkflowDef | null>(null);
  const [loadingDef, setLoadingDef] = useState(false);
  const [running, setRunning] = useState<string | null>(null);
  const [accountId, setAccountId] = useState("");

  useEffect(() => {
    api.workflows
      .list()
      .then((r) => setFiles(r.workflows))
      .catch(() => setFiles([]));
  }, []);

  async function openDef(name: string) {
    setActive(name);
    setLoadingDef(true);
    setDef(null);
    try {
      const r = await api.workflows.get(name);
      setDef(r.workflow as WorkflowDef);
    } catch (e) {
      toast.error(`load failed: ${e instanceof Error ? e.message : "unknown"}`);
    } finally {
      setLoadingDef(false);
    }
  }

  async function runOnce(name: string) {
    setRunning(name);
    try {
      const t = toast.loading(`starting ${name}…`);
      await api.workflows.run(name, {
        account_id: accountId || undefined,
      });
      toast.success(`${name} dispatched`, { id: t });
      setTimeout(refresh, 1000);
    } catch (e) {
      toast.error(`run failed: ${e instanceof Error ? e.message : "unknown"}`);
    } finally {
      setRunning(null);
    }
  }


  const history = useMemo(() => [...recent].reverse(), [recent]);

  return (
    <div className="space-y-5 animate-fade-up">
      <div className="grid gap-4 lg:grid-cols-[1fr,360px]">
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle>Workflow library</CardTitle>
            <Badge tone="muted">{files?.length ?? 0}</Badge>
          </CardHeader>
          <CardContent>
            {files === null ? (
              <div className="grid sm:grid-cols-2 gap-3">
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-24" />
                ))}
              </div>
            ) : files.length === 0 ? (
              <EmptyState
                icon={WorkflowIcon}
                title="No workflows configured"
                description="Drop .json or .yaml files into config/workflows/ and reload."
              />
            ) : (
              <div className="grid sm:grid-cols-2 gap-3">
                {files.map((name) => (
                  <WorkflowCard
                    key={name}
                    name={name}
                    busy={running === name}
                    onView={() => openDef(name)}
                    onRun={() => runOnce(name)}
                  />
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Run controls</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <label className="block">
              <span className="text-[11px] uppercase tracking-widest text-muted-foreground">
                Account ID (optional)
              </span>
              <Input
                value={accountId}
                onChange={(e) => setAccountId(e.target.value)}
                placeholder="leave blank for headless run"
                className="mt-1"
              />
            </label>
            <p className="text-xs text-muted-foreground">
              When an account ID is provided, the run honors that account's
              browser overrides (proxy, UA, viewport, locale, profile_id) and
              records the result against it.
            </p>
            <div className="rounded-md border border-dashed border-border/50 p-3 text-[11px] text-muted-foreground space-y-1">
              <div className="font-semibold text-foreground/80 inline-flex items-center gap-1.5">
                <Repeat className="h-3 w-3" /> Bulk runs
              </div>
              <p>
                Use the API <code className="font-mono">/workflows/&#123;name&#125;/run_for_accounts</code>{" "}
                to dispatch the same workflow across many accounts (sequential
                by default to avoid profile lock contention).
              </p>
            </div>
          </CardContent>
        </Card>
      </div>


      <Card>
        <CardHeader>
          <CardTitle>Execution history</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {history.length === 0 ? (
            <EmptyState
              icon={ListTree}
              title="No runs recorded"
              description="Workflow runs appear here as they finish."
              className="m-5"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-secondary/30 text-[11px] uppercase tracking-wider text-muted-foreground">
                  <tr className="[&>th]:py-2.5 [&>th]:px-3 [&>th]:text-left [&>th]:font-medium">
                    <th>Workflow</th>
                    <th>Status</th>
                    <th className="hidden md:table-cell">Account</th>
                    <th className="hidden lg:table-cell">Profile / proxy</th>
                    <th>Steps</th>
                    <th>Duration</th>
                    <th>Ended</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((r, i) => (
                    <tr
                      key={i}
                      className="border-t border-border/40 hover:bg-secondary/30"
                    >
                      <td className="px-3 py-2 font-medium">
                        {r.workflow ?? "(unknown)"}
                        {r.kind === "batch" && (
                          <Badge tone="accent" className="ml-2">batch</Badge>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <Badge tone={statusTone(r.status)}>{r.status ?? "?"}</Badge>
                      </td>
                      <td className="px-3 py-2 hidden md:table-cell font-mono text-xs">
                        {r.account_id ?? (r.kind === "batch" ? `${r.total ?? 0} accounts` : "—")}
                      </td>
                      <td className="px-3 py-2 hidden lg:table-cell text-xs text-muted-foreground">
                        <div className="truncate max-w-[260px]">
                          {r.profile_id ?? "—"}
                          {r.proxy ? ` · ${r.proxy}` : ""}
                        </div>
                      </td>
                      <td className="px-3 py-2 tabular-nums">
                        {r.kind === "batch"
                          ? `${r.succeeded ?? 0}/${r.total ?? 0}`
                          : (r.steps ?? r.records?.length ?? "—")}
                      </td>
                      <td className="px-3 py-2 tabular-nums text-xs text-muted-foreground">
                        {fmtMs(r.duration_ms)}
                      </td>
                      <td className="px-3 py-2 text-xs text-muted-foreground">
                        {fmtRelativeTime(r.ended_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <DefDialog
        active={active}
        def={def}
        loading={loadingDef}
        onOpenChange={(v) => !v && setActive(null)}
      />
    </div>
  );
}


function WorkflowCard({
  name, onView, onRun, busy,
}: {
  name: string;
  onView: () => void;
  onRun: () => void;
  busy: boolean;
}) {
  const ext = name.split(".").pop()?.toLowerCase();
  return (
    <motion.div
      whileHover={{ y: -2 }}
      className={cn(
        "panel halo p-4 flex flex-col gap-3 transition-shadow",
        "hover:shadow-glow",
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <FileCode2 className="h-3 w-3" />
            <span className="uppercase tracking-widest">{ext ?? "wf"}</span>
          </div>
          <div className="font-medium truncate">{name}</div>
        </div>
        <Badge tone="muted">workflow</Badge>
      </div>
      <div className="mt-auto flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          className="flex-1"
          onClick={onView}
        >
          <Code2 className="h-3.5 w-3.5" /> Inspect
        </Button>
        <Button
          size="sm"
          className="flex-1"
          onClick={onRun}
          disabled={busy}
        >
          <Play className="h-3.5 w-3.5" /> {busy ? "Starting…" : "Run"}
        </Button>
      </div>
    </motion.div>
  );
}


function DefDialog({
  active, def, loading, onOpenChange,
}: {
  active: string | null;
  def: WorkflowDef | null;
  loading: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  return (
    <Dialog open={!!active} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <WorkflowIcon className="h-4 w-4" /> {active}
          </DialogTitle>
          {def?.description && (
            <DialogDescription>{def.description}</DialogDescription>
          )}
        </DialogHeader>

        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-4 w-full" />
            ))}
          </div>
        ) : def ? (
          <Tabs defaultValue="steps">
            <TabsList>
              <TabsTrigger value="steps">Steps ({def.steps?.length ?? 0})</TabsTrigger>
              <TabsTrigger value="inputs">Inputs</TabsTrigger>
              <TabsTrigger value="json">Raw</TabsTrigger>
            </TabsList>
            <TabsContent value="steps">
              <ScrollArea className="h-[420px] -mx-2">
                <ol className="space-y-2 px-2">
                  {(def.steps ?? []).map((s, i) => (
                    <li
                      key={i}
                      className="rounded-md border border-border/60 bg-secondary/30 p-3 flex items-start gap-3"
                    >
                      <span className="grid place-items-center h-6 w-6 rounded-full bg-primary/15 text-primary text-[11px] font-semibold tabular-nums">
                        {i + 1}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-xs text-primary">
                            {String(s.type ?? "step")}
                          </span>
                          {typeof s.name === "string" && (
                            <span className="text-sm">{s.name}</span>
                          )}
                          {typeof s.if === "string" && (
                            <Badge tone="muted">if</Badge>
                          )}
                          {typeof s.retries === "number" && s.retries > 0 && (
                            <Badge tone="warning">retries: {s.retries}</Badge>
                          )}
                        </div>
                        {s.params ? (
                          <pre className="mt-1 text-[11px] text-muted-foreground bg-background/40 rounded p-2 overflow-x-auto">
                            {JSON.stringify(s.params, null, 2)}
                          </pre>
                        ) : null}
                      </div>
                      <ChevronRight className="h-4 w-4 text-muted-foreground" />
                    </li>
                  ))}
                </ol>
              </ScrollArea>
            </TabsContent>
            <TabsContent value="inputs">
              <pre className="text-xs bg-background/60 rounded p-3 overflow-x-auto max-h-[420px]">
                {JSON.stringify(def.inputs ?? {}, null, 2)}
              </pre>
            </TabsContent>
            <TabsContent value="json">
              <pre className="text-xs bg-background/60 rounded p-3 overflow-x-auto max-h-[420px]">
                {JSON.stringify(def, null, 2)}
              </pre>
            </TabsContent>
          </Tabs>
        ) : (
          <div className="text-sm text-muted-foreground">No definition.</div>
        )}
      </DialogContent>
    </Dialog>
  );
}
