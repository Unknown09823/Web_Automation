import { useMemo } from "react";
import {
  Activity, BrainCircuit, Cpu, Gauge as GaugeIcon, MemoryStick, Rocket,
  TrendingDown, TrendingUp, Workflow as WorkflowIcon, Zap,
} from "lucide-react";
import {
  Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Progress } from "@/components/ui/progress";
import { StatCard } from "@/components/stat-card";
import { EmptyState } from "@/components/empty-state";
import {
  useAi, useDataStore, useMetrics, useStatus, useWorkflows,
} from "@/stores/data-store";
import { fmtDuration, fmtNumber, fmtPercent, fmtRelativeTime, statusTone } from "@/lib/format";
import { cn } from "@/lib/utils";


export default function OverviewPage() {
  const status = useStatus();
  const metrics = useMetrics();
  const ai = useAi();
  const workflows = useWorkflows();
  const samples = useDataStore((s) => s.samples);
  const loading = useDataStore((s) => s.loading);

  const accounts = status?.accounts ?? null;
  const successRate = accounts?.completion_rate ?? 0;
  const failureRate = accounts?.failure_rate ?? 0;

  const wfChart = useMemo(() => {
    return samples.map((s, i) => ({
      i,
      cpu: s.cpu,
      mem: s.memory,
      runs: s.workflowRuns,
    }));
  }, [samples]);

  const recent = useMemo(() => {
    return [...workflows].reverse().slice(0, 8);
  }, [workflows]);

  return (
    <div className="space-y-6 animate-fade-up">
      <Hero status={status} ai={ai} loading={loading} />

      <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          label="Active workflows"
          icon={WorkflowIcon}
          tone="primary"
          value={fmtNumber(status?.scheduler.jobs.length ?? 0)}
          hint={
            status?.scheduler.max_workers
              ? `${status.scheduler.max_workers} workers`
              : "scheduler idle"
          }
          loading={loading && !status}
        />
        <StatCard
          label="Running tasks"
          icon={Activity}
          tone="accent"
          value={fmtNumber(metrics?.tasks.by_status.running ?? 0)}
          hint={`${fmtNumber(metrics?.tasks.total ?? 0)} total tracked`}
          loading={loading && !metrics}
        />
        <StatCard
          label="Success rate"
          icon={TrendingUp}
          tone="success"
          halo
          value={fmtPercent(successRate * 100)}
          hint={`${fmtNumber(accounts?.completed ?? 0)} accounts complete`}
          loading={loading && !accounts}
        />
        <StatCard
          label="Failure rate"
          icon={TrendingDown}
          tone="destructive"
          value={fmtPercent(failureRate * 100)}
          hint={`${fmtNumber(accounts?.failed ?? 0)} failed · ${fmtNumber(accounts?.rejected ?? 0)} rejected`}
          loading={loading && !accounts}
        />
      </section>


      <section className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle>Resource usage</CardTitle>
            <span className="text-[11px] text-muted-foreground">last {samples.length} samples</span>
          </CardHeader>
          <CardContent className="pt-0">
            {samples.length < 2 ? (
              <Skeleton className="h-56 w-full" />
            ) : (
              <ResponsiveContainer width="100%" height={240}>
                <AreaChart data={wfChart} margin={{ top: 10, right: 10, left: -16, bottom: 0 }}>
                  <defs>
                    <linearGradient id="cpu" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="hsl(var(--primary))" stopOpacity={0.5} />
                      <stop offset="100%" stopColor="hsl(var(--primary))" stopOpacity={0} />
                    </linearGradient>
                    <linearGradient id="mem" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="hsl(var(--accent))" stopOpacity={0.5} />
                      <stop offset="100%" stopColor="hsl(var(--accent))" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="i" hide />
                  <YAxis
                    domain={[0, 100]}
                    width={36}
                    tickLine={false}
                    axisLine={false}
                    tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                  />
                  <Tooltip
                    cursor={{ stroke: "hsl(var(--border))" }}
                    contentStyle={{
                      background: "hsl(var(--popover))",
                      border: "1px solid hsl(var(--border))",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    formatter={(v: number) => `${v.toFixed(1)}%`}
                  />
                  <Area
                    type="monotone"
                    dataKey="cpu"
                    name="CPU"
                    stroke="hsl(var(--primary))"
                    fill="url(#cpu)"
                    strokeWidth={1.6}
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="mem"
                    name="Memory"
                    stroke="hsl(var(--accent))"
                    fill="url(#mem)"
                    strokeWidth={1.6}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}
            <div className="mt-3 grid grid-cols-3 gap-3 text-xs">
              <ResourceTile icon={Cpu} label="CPU" value={metrics?.system.cpu_percent} />
              <ResourceTile
                icon={MemoryStick}
                label="Memory"
                value={metrics?.system.memory_percent}
                sub={`${fmtNumber(metrics?.system.memory_used_mb ?? 0)} MB`}
              />
              <ResourceTile icon={GaugeIcon} label="Disk" value={metrics?.system.disk_percent} />
            </div>
          </CardContent>
        </Card>

        <AIPanel />
      </section>


      <section className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Recent workflow runs</CardTitle>
          </CardHeader>
          <CardContent className="pt-0">
            {recent.length === 0 ? (
              <EmptyState
                icon={WorkflowIcon}
                title="No workflow runs yet"
                description="Trigger a workflow from the Workflows page to see results here."
              />
            ) : (
              <ul className="divide-y divide-border/50 -mx-1">
                {recent.map((r, i) => (
                  <li
                    key={i}
                    className="flex items-center gap-3 px-1 py-2 text-sm"
                  >
                    <span
                      className={cn(
                        "h-1.5 w-1.5 rounded-full shrink-0",
                        statusTone(r.status) === "success" && "bg-success",
                        statusTone(r.status) === "destructive" && "bg-destructive",
                        statusTone(r.status) === "primary" && "bg-primary",
                        statusTone(r.status) === "warning" && "bg-warning",
                        statusTone(r.status) === "muted" && "bg-muted-foreground/60",
                      )}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="font-medium truncate">{r.workflow ?? "(unknown)"}</div>
                      <div className="text-[11px] text-muted-foreground truncate">
                        {r.account_id ? `account: ${r.account_id}` : r.kind === "batch" ? `batch · ${r.total} accounts` : "no account"}
                      </div>
                    </div>
                    <Badge tone={statusTone(r.status)}>{r.status ?? "?"}</Badge>
                    <span className="text-[11px] text-muted-foreground tabular-nums w-16 text-right">
                      {r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : "—"}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Account progress</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {accounts ? (
              <>
                <div className="grid grid-cols-3 gap-3">
                  <MiniStat label="Total" value={fmtNumber(accounts.total)} />
                  <MiniStat
                    label="Pending"
                    value={fmtNumber(accounts.pending)}
                    tone="warning"
                  />
                  <MiniStat
                    label="Running"
                    value={fmtNumber(accounts.running)}
                    tone="primary"
                  />
                  <MiniStat
                    label="Done"
                    value={fmtNumber(accounts.completed)}
                    tone="success"
                  />
                  <MiniStat
                    label="Failed"
                    value={fmtNumber(accounts.failed)}
                    tone="destructive"
                  />
                  <MiniStat
                    label="Speed"
                    value={`${(accounts.speed_per_minute ?? 0).toFixed(2)}/m`}
                    tone="accent"
                  />
                </div>
                <div className="space-y-1.5">
                  <div className="flex justify-between text-xs">
                    <span className="text-muted-foreground">Completion</span>
                    <span className="tabular-nums">
                      {fmtPercent((accounts.completion_rate ?? 0) * 100)}
                    </span>
                  </div>
                  <Progress value={(accounts.completion_rate ?? 0) * 100} tone="success" />
                </div>
                <div className="space-y-1.5">
                  <div className="flex justify-between text-xs">
                    <span className="text-muted-foreground">Failure</span>
                    <span className="tabular-nums">
                      {fmtPercent((accounts.failure_rate ?? 0) * 100)}
                    </span>
                  </div>
                  <Progress value={(accounts.failure_rate ?? 0) * 100} tone="destructive" />
                </div>
              </>
            ) : (
              <EmptyState
                icon={Rocket}
                title="No accounts loaded"
                description="Configure accounts.json and reload to see account progress."
              />
            )}
          </CardContent>
        </Card>
      </section>
    </div>
  );
}


function Hero({
  status, ai, loading,
}: {
  status: ReturnType<typeof useStatus>;
  ai: ReturnType<typeof useAi>;
  loading: boolean;
}) {
  return (
    <div className="panel halo p-5 md:p-7 relative overflow-hidden">
      <div className="absolute inset-0 grid-bg opacity-30 pointer-events-none [mask-image:radial-gradient(ellipse_at_center,_black_50%,_transparent_100%)]" />
      <div className="relative flex flex-col md:flex-row md:items-end gap-5">
        <div className="flex-1 min-w-0">
          <div className="text-xs uppercase tracking-widest text-muted-foreground">
            Control center
          </div>
          <h1 className="mt-1 text-2xl md:text-3xl font-semibold tracking-tight text-balance">
            {loading
              ? "Booting…"
              : status?.running
                ? "Engine is live"
                : "Engine is idle"}
          </h1>
          <p className="mt-2 text-sm text-muted-foreground max-w-xl text-balance">
            {status?.running
              ? `Up ${fmtDuration(status.uptime_seconds)} · ${status.scheduler.max_workers} workers · ${status.queues ? Object.keys(status.queues).length : 0} queues`
              : "Press the engine button or open the command palette to start."}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={status?.running ? "success" : "muted"}>
            {status?.running ? "running" : "stopped"}
          </Badge>
          <Badge tone={ai?.enabled ? "accent" : "muted"}>
            <BrainCircuit className="h-3 w-3" />
            {ai?.enabled ? "AI enabled" : "AI off"}
          </Badge>
          <Badge tone={status?.ai_enabled ? "primary" : "muted"}>
            <Zap className="h-3 w-3" />
            {fmtNumber(status?.browser_sessions.length ?? 0)} sessions
          </Badge>
          <Badge>
            updated {fmtRelativeTime(Date.now() / 1000)}
          </Badge>
        </div>
      </div>
    </div>
  );
}

function ResourceTile({
  icon: Icon, label, value, sub,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: number | undefined;
  sub?: string;
}) {
  const v = value ?? 0;
  const tone =
    v >= 90 ? "destructive" : v >= 70 ? "warning" : "success";
  return (
    <div className="rounded-lg border border-border/60 bg-secondary/30 p-3">
      <div className="flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-widest text-muted-foreground inline-flex items-center gap-1.5">
          <Icon className="h-3 w-3" />
          {label}
        </span>
        <span className="font-semibold tabular-nums text-sm">
          {value === undefined ? "—" : `${v.toFixed(0)}%`}
        </span>
      </div>
      <Progress value={v} tone={tone} className="mt-2" />
      {sub && <div className="mt-1 text-[10px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

function MiniStat({
  label, value, tone = "muted",
}: {
  label: string;
  value: React.ReactNode;
  tone?: "primary" | "success" | "warning" | "destructive" | "accent" | "muted";
}) {
  const toneClass: Record<typeof tone, string> = {
    primary: "text-primary",
    success: "text-success",
    warning: "text-warning",
    destructive: "text-destructive",
    accent: "text-accent",
    muted: "text-foreground",
  };
  return (
    <div className="rounded-lg border border-border/60 bg-secondary/30 p-3 text-center">
      <div className={cn("text-lg font-semibold tabular-nums", toneClass[tone])}>
        {value}
      </div>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
        {label}
      </div>
    </div>
  );
}

function AIPanel() {
  const ai = useAi();
  const last = ai?.last_decision;
  const stepCount = last?.plan?.steps?.length ?? 0;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <BrainCircuit className="h-3.5 w-3.5 text-accent" /> AI activity
        </CardTitle>
        <Badge tone={ai?.enabled ? "accent" : "muted"}>
          {ai?.enabled ? "online" : "offline"}
        </Badge>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-3 gap-3">
          <MiniStat
            label="Memory"
            value={ai?.memory ? "ON" : "—"}
            tone={ai?.memory ? "success" : "muted"}
          />
          <MiniStat
            label="Dry run"
            value={ai?.dry_run ? "YES" : "NO"}
            tone={ai?.dry_run ? "warning" : "muted"}
          />
          <MiniStat label="Plan steps" value={fmtNumber(stepCount)} tone="primary" />
        </div>
        {last ? (
          <div className="rounded-lg border border-border/60 bg-secondary/30 p-3 space-y-1">
            <div className="flex items-center justify-between text-xs">
              <span className="text-muted-foreground">Last goal</span>
              <Badge tone={last.success ? "success" : "destructive"}>
                {last.success ? "succeeded" : "failed"}
              </Badge>
            </div>
            <div className="text-sm font-medium truncate">{last.goal}</div>
            {last.intent && (
              <div className="text-[11px] text-muted-foreground">
                intent: <span className="font-mono">{last.intent}</span>
              </div>
            )}
          </div>
        ) : (
          <EmptyState
            icon={BrainCircuit}
            title="No decisions yet"
            description="Run a workflow with an ai_goal step to populate this panel."
          />
        )}
      </CardContent>
    </Card>
  );
}
