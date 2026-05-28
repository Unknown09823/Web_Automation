import { motion } from "framer-motion";
import { ArrowDownRight, ArrowUpRight } from "lucide-react";

import { cn } from "@/lib/utils";
import { Skeleton } from "@/components/ui/skeleton";

export interface StatCardProps {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  icon?: React.ComponentType<{ className?: string }>;
  tone?: "primary" | "success" | "warning" | "destructive" | "accent" | "muted";
  delta?: { value: number; suffix?: string };
  loading?: boolean;
  halo?: boolean;
  className?: string;
}

const TONE_BG: Record<NonNullable<StatCardProps["tone"]>, string> = {
  primary: "from-primary/15 to-primary/5 text-primary",
  success: "from-success/15 to-success/5 text-success",
  warning: "from-warning/15 to-warning/5 text-warning",
  destructive: "from-destructive/15 to-destructive/5 text-destructive",
  accent: "from-accent/15 to-accent/5 text-accent",
  muted: "from-secondary/40 to-secondary/10 text-muted-foreground",
};

export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  tone = "muted",
  delta,
  loading,
  halo,
  className,
}: StatCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      className={cn(
        "panel relative overflow-hidden p-5",
        halo && "halo",
        className,
      )}
    >
      <div
        className={cn(
          "absolute -right-8 -top-8 h-32 w-32 rounded-full blur-3xl opacity-50 bg-gradient-to-br",
          TONE_BG[tone],
        )}
        aria-hidden
      />
      <div className="relative flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
          {label}
        </span>
        {Icon && (
          <span
            className={cn(
              "grid place-items-center h-8 w-8 rounded-md border border-border/60 bg-background/40",
              tone !== "muted" && `text-${tone}`,
            )}
          >
            <Icon className="h-4 w-4" />
          </span>
        )}
      </div>
      <div className="relative mt-3 flex items-end gap-2">
        <div className="text-3xl font-semibold tracking-tight text-balance">
          {loading ? <Skeleton className="h-8 w-24" /> : value}
        </div>
        {delta && !loading && (
          <span
            className={cn(
              "inline-flex items-center gap-0.5 text-xs font-medium pb-1",
              delta.value >= 0 ? "text-success" : "text-destructive",
            )}
          >
            {delta.value >= 0 ? (
              <ArrowUpRight className="h-3.5 w-3.5" />
            ) : (
              <ArrowDownRight className="h-3.5 w-3.5" />
            )}
            {Math.abs(delta.value).toFixed(1)}
            {delta.suffix ?? "%"}
          </span>
        )}
      </div>
      {hint && (
        <div className="relative mt-2 text-xs text-muted-foreground">{hint}</div>
      )}
    </motion.div>
  );
}
