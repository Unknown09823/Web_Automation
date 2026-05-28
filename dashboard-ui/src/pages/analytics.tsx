import { useMemo } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { BarChart3, PieChart as PieIcon, TimerReset, TrendingUp } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/empty-state";
import { useDataStore, useWorkflows } from "@/stores/data-store";
import { fmtMs, fmtNumber, fmtPercent } from "@/lib/format";
import { Badge } from "@/components/ui/badge";

const CHART_COLORS = [
  "hsl(var(--primary))",
  "hsl(var(--accent))",
  "hsl(var(--success))",
  "hsl(var(--warning))",
  "hsl(var(--destructive))",
];


export default function AnalyticsPage() {
  const samples = useDataStore((s) => s.samples);
  const workflows = useWorkflows();

  const ts = useMemo(() => {
    return samples.map((s, i) => ({
      i,
      t: new Date(s.ts).toLocaleTimeString(),
      cpu: s.cpu,
      mem: s.memory,
      runs: s.workflowRuns,
    }));
  }, [samples]);

  const perWorkflow = useMemo(() => {
    const m = new Map<string, { name: string; runs: number; ok: number; fail: number; durMs: number }>();
    for (const w of workflows) {
      if (!w.workflow) continue;
      const cur = m.get(w.workflow) ?? { name: w.workflow, runs: 0, ok: 0, fail: 0, durMs: 0 };
      cur.runs += 1;
      if (w.status === "succeeded") cur.ok += 1;
      else if (w.status && w.status !== "running") cur.fail += 1;
      if (typeof w.duration_ms === "number") cur.durMs += w.duration_ms;
      m.set(w.workflow, cur);
    }
    return [...m.values()]
      .map((r) => ({ ...r, avgMs: r.runs ? r.durMs / r.runs : 0, success: r.runs ? (r.ok / r.runs) * 100 : 0 }))
      .sort((a, b) => b.runs - a.runs);
  }, [workflows]);

  const statusMix = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const w of workflows) {
      const k = w.status ?? "unknown";
      counts[k] = (counts[k] ?? 0) + 1;
    }
    return Object.entries(counts).map(([name, value]) => ({ name, value }));
  }, [workflows]);


  return (
    <div className="space-y-5 animate-fade-up">
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <TrendingUp className="h-3.5 w-3.5" /> Resource trend
            </CardTitle>
            <Badge tone="muted">{ts.length} samples</Badge>
          </CardHeader>
          <CardContent>
            {ts.length < 2 ? (
              <EmptyState
                icon={TrendingUp}
                title="Collecting samples"
                description="Trend chart appears once a few polls complete."
              />
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <LineChart data={ts} margin={{ top: 10, right: 10, left: -16, bottom: 0 }}>
                  <CartesianGrid stroke="hsl(var(--border))" strokeOpacity={0.3} vertical={false} />
                  <XAxis
                    dataKey="t"
                    tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    axisLine={false}
                    tickLine={false}
                    minTickGap={32}
                  />
                  <YAxis
                    domain={[0, 100]}
                    width={36}
                    tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "hsl(var(--popover))",
                      border: "1px solid hsl(var(--border))",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    formatter={(v: number) => `${v.toFixed(1)}%`}
                  />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Line type="monotone" dataKey="cpu" stroke="hsl(var(--primary))" strokeWidth={1.6} dot={false} isAnimationActive={false} />
                  <Line type="monotone" dataKey="mem" stroke="hsl(var(--accent))" strokeWidth={1.6} dot={false} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <PieIcon className="h-3.5 w-3.5" /> Run status mix
            </CardTitle>
          </CardHeader>
          <CardContent>
            {statusMix.length === 0 ? (
              <EmptyState
                icon={PieIcon}
                title="No runs yet"
                description="Status breakdown will appear after workflows execute."
              />
            ) : (
              <ResponsiveContainer width="100%" height={240}>
                <PieChart>
                  <Pie
                    data={statusMix}
                    dataKey="value"
                    nameKey="name"
                    innerRadius={50}
                    outerRadius={80}
                    paddingAngle={2}
                    isAnimationActive={false}
                  >
                    {statusMix.map((_, i) => (
                      <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{
                      background: "hsl(var(--popover))",
                      border: "1px solid hsl(var(--border))",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                  />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                </PieChart>
              </ResponsiveContainer>
            )}
          </CardContent>
        </Card>
      </div>


      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <BarChart3 className="h-3.5 w-3.5" /> Workflow performance
          </CardTitle>
        </CardHeader>
        <CardContent>
          {perWorkflow.length === 0 ? (
            <EmptyState
              icon={BarChart3}
              title="No workflow data"
              description="Run a few workflows to populate this view."
            />
          ) : (
            <div className="grid gap-4 lg:grid-cols-2">
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={perWorkflow} margin={{ top: 10, right: 10, left: -16, bottom: 0 }}>
                  <CartesianGrid stroke="hsl(var(--border))" strokeOpacity={0.3} vertical={false} />
                  <XAxis
                    dataKey="name"
                    tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }}
                    axisLine={false}
                    tickLine={false}
                    interval={0}
                    angle={-15}
                    height={50}
                  />
                  <YAxis tick={{ fontSize: 10, fill: "hsl(var(--muted-foreground))" }} axisLine={false} tickLine={false} width={28} />
                  <Tooltip
                    contentStyle={{
                      background: "hsl(var(--popover))",
                      border: "1px solid hsl(var(--border))",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                  />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Bar dataKey="ok" stackId="a" fill="hsl(var(--success))" name="succeeded" />
                  <Bar dataKey="fail" stackId="a" fill="hsl(var(--destructive))" name="failed" />
                </BarChart>
              </ResponsiveContainer>

              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="text-[11px] uppercase tracking-wider text-muted-foreground">
                    <tr className="[&>th]:py-2 [&>th]:text-left">
                      <th>Workflow</th>
                      <th>Runs</th>
                      <th>Success</th>
                      <th>Avg</th>
                    </tr>
                  </thead>
                  <tbody>
                    {perWorkflow.map((r) => (
                      <tr key={r.name} className="border-t border-border/40">
                        <td className="py-2 font-medium truncate max-w-[180px]">{r.name}</td>
                        <td className="py-2 tabular-nums">{fmtNumber(r.runs)}</td>
                        <td className="py-2 tabular-nums">{fmtPercent(r.success, 0)}</td>
                        <td className="py-2 tabular-nums text-muted-foreground inline-flex items-center gap-1">
                          <TimerReset className="h-3 w-3" />
                          {fmtMs(r.avgMs)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
