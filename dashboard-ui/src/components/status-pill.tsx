import { motion } from "framer-motion";
import { Wifi, WifiOff } from "lucide-react";

import { cn } from "@/lib/utils";
import { useDataStore } from "@/stores/data-store";

/**
 * Live status pill for the topbar. Reflects four states:
 *   ok          — engine running, last fetch succeeded
 *   degraded    — engine reachable but flagged warn/error
 *   stopped     — engine reachable but not running
 *   offline     — last fetch failed (network/API down)
 */
export function StatusPill() {
  const status = useDataStore((s) => s.status);
  const online = useDataStore((s) => s.online);
  const lastError = useDataStore((s) => s.lastError);

  let label = "checking";
  let tone: "ok" | "warn" | "err" | "muted" = "muted";

  if (!online) {
    label = "offline";
    tone = "err";
  } else if (status?.running) {
    if (status.status === "ok") {
      label = "live";
      tone = "ok";
    } else {
      label = status.status || "degraded";
      tone = "warn";
    }
  } else if (status) {
    label = "stopped";
    tone = "warn";
  }

  const dotColor =
    tone === "ok"
      ? "bg-success text-success"
      : tone === "warn"
        ? "bg-warning text-warning"
        : tone === "err"
          ? "bg-destructive text-destructive"
          : "bg-muted-foreground text-muted-foreground";

  return (
    <motion.div
      layout
      title={lastError ?? "engine status"}
      className={cn(
        "inline-flex items-center gap-2 rounded-full border border-border/60 bg-background/40 backdrop-blur px-2.5 py-1 text-[11px] font-medium",
      )}
    >
      <span className="relative inline-flex">
        <span className={cn("dot", dotColor)} />
        {tone === "ok" && (
          <span className={cn("dot live absolute inset-0", dotColor)} />
        )}
      </span>
      <span className="uppercase tracking-wider">{label}</span>
      {tone === "err" ? (
        <WifiOff className="h-3 w-3 opacity-70" />
      ) : (
        <Wifi className="h-3 w-3 opacity-50" />
      )}
    </motion.div>
  );
}
