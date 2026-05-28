import { useEffect, useMemo, useState } from "react";
import {
  CheckCircle2, Filter, Loader2, MoreHorizontal, Pause, Play, RotateCw, Search,
  ShieldAlert, Trash2, Unlock, Users,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { EmptyState } from "@/components/empty-state";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api } from "@/lib/api";
import { useAccounts, useDataStore } from "@/stores/data-store";
import { fmtNumber, fmtPercent, fmtRelativeTime, statusTone } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { AccountRow } from "@/types/api";


const STATUS_FILTERS = [
  "all", "pending", "running", "completed", "failed", "skipped", "paused",
] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];

export default function AccountsPage() {
  const accounts = useAccounts();
  const refresh = useDataStore((s) => s.refreshAccounts);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 6000);
    return () => clearInterval(id);
  }, [refresh]);

  const rows = accounts?.accounts ?? [];
  const stats = (accounts?.stats ?? {}) as {
    total?: number; pending?: number; running?: number; completed?: number;
    failed?: number; skipped?: number; rejected?: number;
    completion_rate?: number; failure_rate?: number; speed_per_minute?: number;
  };


  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((a) => {
      if (statusFilter !== "all" && a.status !== statusFilter) return false;
      if (!q) return true;
      const hay = [a.id, a.username, a.email, a.last_workflow, a.last_error]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
  }, [rows, query, statusFilter]);

  const allSelected =
    filtered.length > 0 && filtered.every((r) => selected.has(r.id));

  function toggleAll() {
    if (allSelected) {
      const next = new Set(selected);
      for (const r of filtered) next.delete(r.id);
      setSelected(next);
    } else {
      const next = new Set(selected);
      for (const r of filtered) next.add(r.id);
      setSelected(next);
    }
  }

  function toggleOne(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  async function bulk(
    action: "reset" | "pause" | "resume" | "release",
  ): Promise<void> {
    if (selected.size === 0) return;
    setBusy(true);
    const ids = Array.from(selected);
    const t = toast.loading(`${action} ${ids.length} account(s)…`);
    let ok = 0;
    let fail = 0;
    const fn = api.accounts[action] as (id: string) => Promise<unknown>;
    for (const id of ids) {
      try {
        await fn(id);
        ok += 1;
      } catch {
        fail += 1;
      }
    }
    toast[fail ? "warning" : "success"](
      `${action}: ${ok} ok${fail ? ` · ${fail} failed` : ""}`,
      { id: t },
    );
    setSelected(new Set());
    await refresh();
    setBusy(false);
  }

  async function reloadFile() {
    try {
      const t = toast.loading("reloading accounts file…");
      const r = await api.accounts.reload();
      toast.success(`loaded ${r.loaded} (${r.rejected} rejected)`, { id: t });
      refresh();
    } catch (e) {
      toast.error(`reload failed: ${e instanceof Error ? e.message : "unknown"}`);
    }
  }


  return (
    <div className="space-y-5 animate-fade-up">
      <Card>
        <CardHeader>
          <CardTitle>Account fleet</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-3">
          <SummaryRow stats={stats} />
          <div className="space-y-2 md:col-span-2">
            <Bar label="Completion" value={(stats.completion_rate ?? 0) * 100} tone="success" />
            <Bar label="Failure" value={(stats.failure_rate ?? 0) * 100} tone="destructive" />
            <Bar
              label="Throughput"
              value={Math.min(100, (stats.speed_per_minute ?? 0) * 20)}
              tone="primary"
              caption={`${(stats.speed_per_minute ?? 0).toFixed(2)} accts/min`}
            />
          </div>
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[220px] max-w-md">
          <Search className="h-3.5 w-3.5 absolute left-3 top-3 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search id, username, email, error…"
            className="pl-9"
          />
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm" className="gap-2">
              <Filter className="h-3.5 w-3.5" />
              {statusFilter === "all" ? "All statuses" : statusFilter}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuLabel>Status</DropdownMenuLabel>
            <DropdownMenuSeparator />
            {STATUS_FILTERS.map((s) => (
              <DropdownMenuItem
                key={s}
                onSelect={() => setStatusFilter(s)}
                className="capitalize"
              >
                {s}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <div className="ml-auto flex items-center gap-2">
          {selected.size > 0 ? (
            <>
              <Badge tone="primary">{selected.size} selected</Badge>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => bulk("pause")}>
                <Pause className="h-3.5 w-3.5" /> Pause
              </Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => bulk("resume")}>
                <Play className="h-3.5 w-3.5" /> Resume
              </Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => bulk("release")}>
                <Unlock className="h-3.5 w-3.5" /> Release
              </Button>
              <Button size="sm" variant="destructive" disabled={busy} onClick={() => bulk("reset")}>
                <RotateCw className="h-3.5 w-3.5" /> Reset
              </Button>
            </>
          ) : (
            <>
              <Button size="sm" variant="outline" onClick={reloadFile}>
                <RotateCw className="h-3.5 w-3.5" /> Reload file
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={async () => {
                  try {
                    const r = await api.accounts.reapLocks();
                    toast.success(`released ${r.released} locks`);
                    refresh();
                  } catch (e) {
                    toast.error(`reap failed: ${e instanceof Error ? e.message : "unknown"}`);
                  }
                }}
              >
                <Unlock className="h-3.5 w-3.5" /> Reap locks
              </Button>
            </>
          )}
        </div>
      </div>


      <Card className="overflow-hidden p-0">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-secondary/30 text-[11px] uppercase tracking-wider text-muted-foreground">
              <tr className="[&>th]:py-2.5 [&>th]:px-3 [&>th]:text-left [&>th]:font-medium">
                <th className="w-8">
                  <input
                    type="checkbox"
                    aria-label="select all"
                    checked={allSelected}
                    onChange={toggleAll}
                    className="accent-primary"
                  />
                </th>
                <th>ID</th>
                <th className="hidden md:table-cell">Username</th>
                <th className="hidden lg:table-cell">Email</th>
                <th>Status</th>
                <th className="hidden md:table-cell">Attempts</th>
                <th className="hidden xl:table-cell">Last workflow</th>
                <th className="hidden lg:table-cell">Updated</th>
                <th className="w-10"></th>
              </tr>
            </thead>
            <tbody>
              {!accounts ? (
                Array.from({ length: 8 }).map((_, i) => (
                  <tr key={i} className="border-t border-border/40">
                    {Array.from({ length: 9 }).map((_, j) => (
                      <td key={j} className="p-3">
                        <Skeleton className="h-3 w-full" />
                      </td>
                    ))}
                  </tr>
                ))
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={9}>
                    <EmptyState
                      icon={Users}
                      title="No accounts match"
                      description="Adjust filters or load accounts via the API."
                    />
                  </td>
                </tr>
              ) : (
                filtered.map((a) => (
                  <AccountRowView
                    key={a.id}
                    a={a}
                    selected={selected.has(a.id)}
                    onToggle={() => toggleOne(a.id)}
                    onAction={async (action) => {
                      try {
                        const t = toast.loading(`${action} ${a.id}…`);
                        const fn = api.accounts[action] as (id: string) => Promise<unknown>;
                        await fn(a.id);
                        toast.success(`${action} ok`, { id: t });
                        refresh();
                      } catch (e) {
                        toast.error(
                          `${action} failed: ${e instanceof Error ? e.message : "unknown"}`,
                        );
                      }
                    }}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="text-[11px] text-muted-foreground text-right">
        showing {filtered.length} of {rows.length}
      </div>
    </div>
  );
}


function SummaryRow({
  stats,
}: {
  stats: {
    total?: number; pending?: number; running?: number; completed?: number;
    failed?: number; rejected?: number;
  };
}) {
  return (
    <div className="grid grid-cols-3 gap-3">
      <Cell label="Total" value={fmtNumber(stats.total ?? 0)} icon={Users} />
      <Cell label="Pending" value={fmtNumber(stats.pending ?? 0)} tone="warning" />
      <Cell label="Running" value={fmtNumber(stats.running ?? 0)} tone="primary" icon={Loader2} />
      <Cell label="Done" value={fmtNumber(stats.completed ?? 0)} tone="success" icon={CheckCircle2} />
      <Cell label="Failed" value={fmtNumber(stats.failed ?? 0)} tone="destructive" icon={ShieldAlert} />
      <Cell label="Rejected" value={fmtNumber(stats.rejected ?? 0)} tone="muted" icon={Trash2} />
    </div>
  );
}

function Cell({
  label, value, tone = "muted", icon: Icon,
}: {
  label: string;
  value: React.ReactNode;
  tone?: "primary" | "success" | "warning" | "destructive" | "muted";
  icon?: React.ComponentType<{ className?: string }>;
}) {
  const toneText: Record<typeof tone, string> = {
    primary: "text-primary",
    success: "text-success",
    warning: "text-warning",
    destructive: "text-destructive",
    muted: "text-foreground",
  };
  return (
    <div className="rounded-lg border border-border/60 bg-secondary/30 p-3 flex items-center gap-3">
      {Icon && (
        <span className={cn("h-7 w-7 grid place-items-center rounded-md bg-background/50 border border-border/60", toneText[tone])}>
          <Icon className="h-3.5 w-3.5" />
        </span>
      )}
      <div className="leading-tight">
        <div className={cn("text-base font-semibold tabular-nums", toneText[tone])}>{value}</div>
        <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{label}</div>
      </div>
    </div>
  );
}

function Bar({
  label, value, tone, caption,
}: {
  label: string;
  value: number;
  tone: "primary" | "success" | "warning" | "destructive";
  caption?: string;
}) {
  return (
    <div>
      <div className="flex justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="tabular-nums">
          {caption ?? fmtPercent(value)}
        </span>
      </div>
      <Progress value={value} tone={tone} className="mt-1" />
    </div>
  );
}


function AccountRowView({
  a, selected, onToggle, onAction,
}: {
  a: AccountRow;
  selected: boolean;
  onToggle: () => void;
  onAction: (action: "reset" | "pause" | "resume" | "release") => void;
}) {
  const tone = statusTone(a.status);
  const attempts = a.attempts ?? 0;
  const attemptPct = Math.min(100, attempts * 25);
  return (
    <tr
      className={cn(
        "border-t border-border/40 transition-colors",
        selected ? "bg-primary/5" : "hover:bg-secondary/30",
      )}
    >
      <td className="px-3 py-2">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggle}
          aria-label={`select ${a.id}`}
          className="accent-primary"
        />
      </td>
      <td className="px-3 py-2 font-mono text-xs text-foreground/90 whitespace-nowrap">
        {a.id}
      </td>
      <td className="px-3 py-2 hidden md:table-cell truncate max-w-[160px]">
        {a.username ?? "—"}
      </td>
      <td className="px-3 py-2 hidden lg:table-cell truncate max-w-[200px] text-muted-foreground">
        {a.email ?? "—"}
      </td>
      <td className="px-3 py-2">
        <Badge tone={tone}>{a.status}</Badge>
      </td>
      <td className="px-3 py-2 hidden md:table-cell">
        <div className="flex items-center gap-2 w-24">
          <Progress
            value={attemptPct}
            tone={attempts >= 3 ? "destructive" : attempts >= 2 ? "warning" : "primary"}
          />
          <span className="text-xs tabular-nums text-muted-foreground">{attempts}</span>
        </div>
      </td>
      <td className="px-3 py-2 hidden xl:table-cell text-xs text-muted-foreground truncate max-w-[160px]">
        {a.last_workflow ?? "—"}
      </td>
      <td className="px-3 py-2 hidden lg:table-cell text-xs text-muted-foreground">
        {fmtRelativeTime(a.updated_at)}
      </td>
      <td className="px-3 py-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label={`actions for ${a.id}`}>
              <MoreHorizontal className="h-3.5 w-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => onAction("pause")}>
              <Pause className="h-3.5 w-3.5" /> Pause
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => onAction("resume")}>
              <Play className="h-3.5 w-3.5" /> Resume
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => onAction("release")}>
              <Unlock className="h-3.5 w-3.5" /> Release lock
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={() => onAction("reset")}
              className="text-destructive"
            >
              <RotateCw className="h-3.5 w-3.5" /> Reset profile
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </td>
    </tr>
  );
}
