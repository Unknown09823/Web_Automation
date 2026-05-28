import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { Command, KeyRound, Menu, Pause, Play, RotateCw, Search } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { StatusPill } from "@/components/status-pill";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useUIStore } from "@/stores/ui-store";
import { useDataStore } from "@/stores/data-store";
import { fmtRelativeTime } from "@/lib/format";
import { setToken, getToken } from "@/lib/api";

const ROUTE_TITLES: Record<string, string> = {
  "/": "Overview",
  "/accounts": "Accounts",
  "/workflows": "Workflows",
  "/ai": "AI Center",
  "/live": "Live Browser",
  "/plugins": "Plugins",
  "/logs": "Logs",
  "/analytics": "Analytics",
  "/infra": "Infrastructure",
};


function isMac() {
  if (typeof navigator === "undefined") return false;
  return /mac/i.test(navigator.platform);
}

function TokenSettings() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(getToken());

  useEffect(() => {
    if (open) setValue(getToken());
  }, [open]);

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="icon" aria-label="API token">
          <KeyRound className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72">
        <DropdownMenuLabel>API token</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <div className="px-2 py-2 space-y-2">
          <Input
            type="password"
            value={value}
            placeholder="paste AUTOMATION_API_TOKEN"
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                setToken(value);
                toast.success("API token saved");
                setOpen(false);
              }
            }}
          />
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              className="flex-1"
              onClick={() => {
                setToken(value);
                toast.success("API token saved");
                setOpen(false);
              }}
            >
              Save
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setToken("");
                setValue("");
                toast("API token cleared");
              }}
            >
              Clear
            </Button>
          </div>
          <div className="text-[11px] text-muted-foreground">
            Stored in <code className="font-mono">localStorage</code>; sent as
            <code className="font-mono"> Authorization: Bearer ...</code>
          </div>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}


function EngineMenu() {
  const status = useDataStore((s) => s.status);
  const fetchAll = useDataStore((s) => s.fetchAll);
  const running = !!status?.running;

  async function run(action: "start" | "stop" | "restart" | "reload") {
    try {
      const t = toast.loading(`engine.${action}…`);
      const fns = api.control as Record<typeof action, () => Promise<unknown>>;
      const r = (await fns[action]()) as { status?: string };
      toast.success(`engine ${r?.status ?? action}`, { id: t });
      setTimeout(fetchAll, 600);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "unknown";
      toast.error(`engine.${action} failed: ${msg}`);
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="gap-2">
          {running ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
          Engine
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-48">
        <DropdownMenuLabel>Engine</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={() => run("start")}
          disabled={running}
        >
          <Play className="h-3.5 w-3.5" /> Start
        </DropdownMenuItem>
        <DropdownMenuItem
          onSelect={() => run("stop")}
          disabled={!running}
        >
          <Pause className="h-3.5 w-3.5" /> Stop
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => run("restart")}>
          <RotateCw className="h-3.5 w-3.5" /> Restart
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => run("reload")}>
          Reload config + plugins
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}


export function Topbar() {
  const setSidebarOpen = useUIStore((s) => s.setSidebarOpen);
  const setPaletteOpen = useUIStore((s) => s.setPaletteOpen);
  const lastFetched = useDataStore((s) => s.lastFetchedAt);
  const loc = useLocation();
  const title = ROUTE_TITLES[loc.pathname] ?? "Control Center";

  return (
    <header
      className={cn(
        "sticky top-0 z-20 h-14 border-b border-border/60",
        "glass-strong",
        "px-3 sm:px-5 flex items-center gap-2",
      )}
    >
      <Button
        variant="ghost"
        size="icon"
        className="lg:hidden"
        aria-label="Open navigation"
        onClick={() => setSidebarOpen(true)}
      >
        <Menu className="h-5 w-5" />
      </Button>

      <div className="flex flex-col leading-tight">
        <span className="text-[10px] uppercase tracking-widest text-muted-foreground">
          {fmtRelativeTime(lastFetched)}
        </span>
        <h1 className="text-sm font-semibold tracking-tight">{title}</h1>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <button
          type="button"
          onClick={() => setPaletteOpen(true)}
          className={cn(
            "hidden sm:inline-flex items-center gap-2 h-9 px-3 rounded-md",
            "border border-border/70 bg-background/40 backdrop-blur",
            "text-xs text-muted-foreground hover:text-foreground hover:border-border transition-colors",
          )}
        >
          <Search className="h-3.5 w-3.5" />
          <span>Search & commands</span>
          <kbd className="ml-3 inline-flex items-center gap-0.5 rounded border border-border/60 bg-secondary/70 px-1.5 py-0.5 font-mono text-[10px]">
            {isMac() ? <Command className="h-3 w-3" /> : "Ctrl"}
            K
          </kbd>
        </button>

        <Button
          variant="outline"
          size="icon"
          className="sm:hidden"
          onClick={() => setPaletteOpen(true)}
          aria-label="Open command palette"
        >
          <Search className="h-4 w-4" />
        </Button>

        <StatusPill />
        <EngineMenu />
        <TokenSettings />
      </div>
    </header>
  );
}
