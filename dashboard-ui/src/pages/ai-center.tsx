import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import {
  BookOpen, BrainCircuit, Clock, History, Map as MapIcon, Sparkles, Target,
} from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/empty-state";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { api } from "@/lib/api";
import { useAi, useWorkflows } from "@/stores/data-store";
import { fmtNumber, fmtRelativeTime, statusTone } from "@/lib/format";
import type { IntentRow, MemoryPage } from "@/types/api";
import { cn } from "@/lib/utils";


export default function AICenterPage() {
  const ai = useAi();
  const recent = useWorkflows();
  const [intents, setIntents] = useState<IntentRow[] | null>(null);
  const [pages, setPages] = useState<MemoryPage[] | null>(null);
  const [stats, setStats] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    api.ai.intents().then((r) => setIntents(r.intents)).catch(() => setIntents([]));
    api.ai.pages(50).then((r) => setPages(r.pages)).catch(() => setPages([]));
    api.ai.stats().then((r) => setStats(r.stats ?? {})).catch(() => setStats({}));
  }, []);

  const last = ai?.last_decision;
  const aiTimeline = recent
    .filter((r) => r.records?.some((rec) => (rec as { type?: string }).type === "ai_goal"))
    .slice(-12)
    .reverse();


  return (
    <div className="space-y-5 animate-fade-up">
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <BrainCircuit className="h-4 w-4 text-accent" /> Latest decision
            </CardTitle>
            <Badge tone={ai?.enabled ? "accent" : "muted"}>
              {ai?.enabled ? "online" : "offline"}
            </Badge>
          </CardHeader>
          <CardContent>
            {!ai ? (
              <Skeleton className="h-32 w-full" />
            ) : !last ? (
              <EmptyState
                icon={Sparkles}
                title="No AI decision recorded"
                description="Add an ai_goal step to a workflow and run it."
              />
            ) : (
              <div className="space-y-3">
                <div className="flex items-center gap-2 flex-wrap">
                  <Badge tone={last.success ? "success" : "destructive"}>
                    {last.success ? "succeeded" : "failed"}
                  </Badge>
                  {last.intent && (
                    <Badge tone="accent">intent: {last.intent}</Badge>
                  )}
                  {last.page_signature && (
                    <Badge tone="primary" className="font-mono normal-case tracking-normal">
                      {last.page_signature.slice(0, 24)}…
                    </Badge>
                  )}
                  <Badge tone="muted">
                    <Clock className="h-3 w-3" />
                    {fmtRelativeTime(last.ts)}
                  </Badge>
                </div>
                <div>
                  <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
                    Goal
                  </div>
                  <div className="text-sm font-medium">{last.goal ?? "—"}</div>
                </div>
                {last.reasoning && (
                  <div>
                    <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
                      Reasoning
                    </div>
                    <p className="text-sm text-foreground/85 leading-relaxed">
                      {last.reasoning}
                    </p>
                  </div>
                )}
                <div>
                  <div className="text-[11px] uppercase tracking-widest text-muted-foreground mb-2">
                    Plan ({last.plan?.steps?.length ?? 0} steps)
                  </div>
                  <ol className="space-y-1.5">
                    {(last.plan?.steps ?? []).slice(0, 8).map((step, i) => (
                      <li
                        key={i}
                        className="text-xs rounded-md border border-border/60 bg-secondary/30 p-2 font-mono"
                      >
                        {JSON.stringify(step)}
                      </li>
                    ))}
                  </ol>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Memory snapshot</CardTitle>
          </CardHeader>
          <CardContent>
            {stats === null ? (
              <Skeleton className="h-32 w-full" />
            ) : (
              <div className="grid grid-cols-2 gap-3">
                {Object.entries(stats).slice(0, 8).map(([k, v]) => (
                  <div
                    key={k}
                    className="rounded-md border border-border/60 bg-secondary/30 p-3"
                  >
                    <div className="text-lg font-semibold tabular-nums">
                      {typeof v === "number" ? fmtNumber(v) : String(v)}
                    </div>
                    <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                      {k}
                    </div>
                  </div>
                ))}
                {Object.keys(stats).length === 0 && (
                  <div className="col-span-2 text-xs text-muted-foreground text-center py-6">
                    Memory disabled or empty.
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>


      <Card>
        <CardHeader>
          <CardTitle>Knowledge & timeline</CardTitle>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="timeline">
            <TabsList>
              <TabsTrigger value="timeline">
                <History className="h-3 w-3" /> Timeline
              </TabsTrigger>
              <TabsTrigger value="intents">
                <Target className="h-3 w-3" /> Intents
              </TabsTrigger>
              <TabsTrigger value="pages">
                <MapIcon className="h-3 w-3" /> Pages
              </TabsTrigger>
            </TabsList>
            <TabsContent value="timeline">
              {aiTimeline.length === 0 ? (
                <EmptyState
                  icon={History}
                  title="No AI runs in history"
                  description="Workflow runs that include an ai_goal step will appear here."
                />
              ) : (
                <ol className="relative pl-6 space-y-3 before:absolute before:left-2 before:top-0 before:bottom-0 before:w-px before:bg-border/50">
                  {aiTimeline.map((r, i) => (
                    <motion.li
                      key={i}
                      initial={{ opacity: 0, x: -8 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: i * 0.03 }}
                      className="relative"
                    >
                      <span
                        className={cn(
                          "absolute -left-[13px] top-2 h-2.5 w-2.5 rounded-full border-2 border-background",
                          statusTone(r.status) === "success" && "bg-success",
                          statusTone(r.status) === "destructive" && "bg-destructive",
                          statusTone(r.status) === "primary" && "bg-primary",
                          statusTone(r.status) === "warning" && "bg-warning",
                        )}
                      />
                      <div className="text-sm font-medium">
                        {r.workflow ?? "(workflow)"}
                      </div>
                      <div className="text-[11px] text-muted-foreground">
                        {r.account_id ? `account: ${r.account_id} · ` : ""}
                        {fmtRelativeTime(r.ended_at)} · {r.records?.length ?? 0} steps
                      </div>
                    </motion.li>
                  ))}
                </ol>
              )}
            </TabsContent>
            <TabsContent value="intents">
              {intents === null ? (
                <Skeleton className="h-32 w-full" />
              ) : intents.length === 0 ? (
                <EmptyState
                  icon={Target}
                  title="No intents registered"
                  description="The AI brain has no intents loaded yet."
                />
              ) : (
                <div className="grid sm:grid-cols-2 gap-2">
                  {intents.map((it) => (
                    <div
                      key={it.name}
                      className="rounded-md border border-border/60 bg-secondary/30 p-3"
                    >
                      <div className="font-medium">{it.name}</div>
                      <div className="text-xs text-muted-foreground line-clamp-2">
                        {it.description || "—"}
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1">
                        {it.keywords.slice(0, 6).map((k) => (
                          <Badge key={k} tone="muted">{k}</Badge>
                        ))}
                      </div>
                      {it.roles.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {it.roles.slice(0, 6).map((r) => (
                            <Badge key={r} tone="accent">{r}</Badge>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </TabsContent>
            <TabsContent value="pages">
              {pages === null ? (
                <Skeleton className="h-32 w-full" />
              ) : pages.length === 0 ? (
                <EmptyState
                  icon={BookOpen}
                  title="No memorized pages"
                  description="The AI memory will populate as workflows run."
                />
              ) : (
                <ScrollArea className="h-[420px] -mx-2">
                  <ul className="space-y-1.5 px-2">
                    {pages.map((p, i) => (
                      <li
                        key={i}
                        className="rounded-md border border-border/60 bg-secondary/30 p-2.5 text-xs flex items-center gap-3"
                      >
                        <span className="font-mono text-[10px] text-primary truncate max-w-[120px]">
                          {String(p.signature ?? "—").slice(0, 16)}
                        </span>
                        <span className="truncate flex-1">{String(p.url ?? p.title ?? "")}</span>
                        {typeof p.count === "number" && (
                          <Badge tone="muted">{p.count}×</Badge>
                        )}
                        <span className="text-muted-foreground">
                          {fmtRelativeTime(p.last_seen)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </ScrollArea>
              )}
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>
    </div>
  );
}
