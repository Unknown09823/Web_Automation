import { useMemo } from "react";
import {
  Activity, AlertTriangle, CheckCircle2, Cpu, HardDrive, MemoryStick, Network,
  Server, Timer,
} from "lucide-react";
import {
  Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Gauge } from "@/components/gauge";
import { useDataStore, useMetrics, useStatus } from "@/stores/data-store";
import { fmtBytes, fmtDuration, fmtNumber } from "@/lib/format";
import { cn } from "@/lib/utils";


export default function InfrastructurePage() {
  const metrics = useMetrics();
  const status = useStatus();
  const samples = useDataStore((s) => s.samples);
  const sys = metrics?.system;

  const cpuSeries = useMemo(
    () => samples.map((s, i) => ({ i, v: s.cpu })),
    [samples],
  );
  const memSeries = useMemo(
    () => samples.map((s, i) => ({ i, v: s.memory })),
    [samples],
  );

  const components = (status?.components ?? {}) as Record<string, unknown>;

  return (
    <div className="space-y-5 animate-fade-up">
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Server className="h-3.5 w-3.5" /> Host health
          </CardTitle>
          <Badge tone={sys?.status === "ok" ? "success" : "warning"}>
            {sys?.status ?? "checking"}
          </Badge>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-3">
          {sys ? (
            <>
              <Gauge value={sys.cpu_percent} label="CPU" sublabel={`${(sys.cpu_percent ?? 0).toFixed(1)}%`} />
              <Gauge
                value={sys.memory_percent}
                label="Memory"
                sublabel={`${fmtNumber(sys.memory_used_mb)} MB`}
              />
              <Gauge value={sys.disk_percent} label="Disk" sublabel={`${(sys.disk_percent ?? 0).toFixed(1)}% used`} />
            </>
          ) : (
            Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-32 w-full" />
            ))
          )}
        </CardContent>
      </Card>


      <div className="grid gap-4 lg:grid-cols-2">
        <TrendCard
          title="CPU"
          icon={Cpu}
          color="hsl(var(--primary))"
          series={cpuSeries}
          unit="%"
        />
        <TrendCard
          title="Memory"
          icon={MemoryStick}
          color="hsl(var(--accent))"
          series={memSeries}
          unit="%"
        />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KV
          icon={Timer}
          label="Uptime"
          value={fmtDuration(sys?.uptime_seconds)}
        />
        <KV
          icon={HardDrive}
          label="Disk usage"
          value={sys ? `${fmtNumber(sys.disk_percent ?? 0, 1)}%` : "—"}
          hint={sys ? "system mount" : ""}
        />
        <KV
          icon={MemoryStick}
          label="RAM used"
          value={sys ? fmtBytes((sys.memory_used_mb ?? 0) * 1024 * 1024) : "—"}
          hint={sys ? `${(sys.memory_percent ?? 0).toFixed(1)}% of total` : ""}
        />
        <KV
          icon={Network}
          label="Browser sessions"
          value={fmtNumber(metrics?.browser_sessions ?? 0)}
          hint={status?.browser_sessions.length ? "live" : "idle"}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Activity className="h-3.5 w-3.5" /> Components
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Object.keys(components).length === 0 ? (
            <div className="text-xs text-muted-foreground sm:col-span-3">
              The engine has not reported component health yet.
            </div>
          ) : (
            Object.entries(components).map(([name, value]) => {
              const ok = JSON.stringify(value).toLowerCase().includes("ok") ||
                value === true;
              return (
                <div
                  key={name}
                  className={cn(
                    "rounded-md border bg-secondary/30 p-3 flex items-center gap-3",
                    ok ? "border-success/30" : "border-warning/30",
                  )}
                >
                  {ok ? (
                    <CheckCircle2 className="h-4 w-4 text-success" />
                  ) : (
                    <AlertTriangle className="h-4 w-4 text-warning" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="font-medium">{name}</div>
                    <div className="text-[11px] text-muted-foreground truncate font-mono">
                      {JSON.stringify(value)}
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </CardContent>
      </Card>
    </div>
  );
}


function TrendCard({
  title, icon: Icon, color, series, unit,
}: {
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  color: string;
  series: { i: number; v: number }[];
  unit: string;
}) {
  const last = series.at(-1)?.v ?? 0;
  const max = Math.max(0, ...series.map((s) => s.v));
  const avg = series.length
    ? series.reduce((a, b) => a + b.v, 0) / series.length
    : 0;
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Icon className="h-3.5 w-3.5" /> {title}
        </CardTitle>
        <span className="text-xs text-muted-foreground tabular-nums">
          peak {max.toFixed(1)}
          {unit} · avg {avg.toFixed(1)}
          {unit}
        </span>
      </CardHeader>
      <CardContent>
        <div className="text-3xl font-semibold tracking-tight tabular-nums">
          {last.toFixed(1)}
          <span className="text-base text-muted-foreground">{unit}</span>
        </div>
        {series.length < 2 ? (
          <Skeleton className="h-32 w-full mt-3" />
        ) : (
          <ResponsiveContainer width="100%" height={130} className="mt-2">
            <AreaChart data={series}>
              <defs>
                <linearGradient id={`g-${title}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.4} />
                  <stop offset="100%" stopColor={color} stopOpacity={0} />
                </linearGradient>
              </defs>
              <YAxis hide domain={[0, 100]} />
              <XAxis hide dataKey="i" />
              <Tooltip
                cursor={false}
                contentStyle={{
                  background: "hsl(var(--popover))",
                  border: "1px solid hsl(var(--border))",
                  borderRadius: 8,
                  fontSize: 12,
                }}
                formatter={(v: number) => `${v.toFixed(1)}${unit}`}
                labelFormatter={() => ""}
              />
              <Area
                type="monotone"
                dataKey="v"
                stroke={color}
                strokeWidth={1.6}
                fill={`url(#g-${title})`}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  );
}

function KV({
  icon: Icon, label, value, hint,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
}) {
  return (
    <div className="panel p-4">
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-widest text-muted-foreground">
        <Icon className="h-3 w-3" />
        {label}
      </div>
      <div className="mt-1 text-xl font-semibold tracking-tight tabular-nums">
        {value}
      </div>
      {hint && <div className="text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  );
}
