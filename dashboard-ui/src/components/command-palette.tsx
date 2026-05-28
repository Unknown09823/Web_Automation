import { useEffect, useState } from "react";
import { Command } from "cmdk";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  BarChart3,
  Boxes,
  BrainCircuit,
  ChevronsUpDown,
  Cpu,
  FileText,
  LayoutGrid,
  Monitor,
  Pause,
  Play,
  RotateCw,
  Search,
  Users,
  Workflow,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { useUIStore } from "@/stores/ui-store";
import { useHotkey } from "@/lib/hotkeys";
import { api } from "@/lib/api";
import { useDataStore } from "@/stores/data-store";


interface NavCmd {
  id: string;
  label: string;
  to: string;
  icon: React.ComponentType<{ className?: string }>;
  keywords?: string[];
}

const NAV_CMDS: NavCmd[] = [
  { id: "go-overview", label: "Go to Overview", to: "/", icon: LayoutGrid, keywords: ["home", "dashboard"] },
  { id: "go-accounts", label: "Go to Accounts", to: "/accounts", icon: Users },
  { id: "go-workflows", label: "Go to Workflows", to: "/workflows", icon: Workflow },
  { id: "go-ai", label: "Go to AI Center", to: "/ai", icon: BrainCircuit, keywords: ["brain", "intent", "memory"] },
  { id: "go-live", label: "Go to Live Browser", to: "/live", icon: Monitor },
  { id: "go-plugins", label: "Go to Plugins", to: "/plugins", icon: Boxes },
  { id: "go-logs", label: "Go to Logs", to: "/logs", icon: FileText },
  { id: "go-analytics", label: "Go to Analytics", to: "/analytics", icon: BarChart3 },
  { id: "go-infra", label: "Go to Infrastructure", to: "/infra", icon: Cpu, keywords: ["health", "ec2", "system"] },
];


export function CommandPalette() {
  const open = useUIStore((s) => s.paletteOpen);
  const setOpen = useUIStore((s) => s.setPaletteOpen);
  const togglePalette = useUIStore((s) => s.togglePalette);
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const fetchAll = useDataStore((s) => s.fetchAll);
  const workflows = useDataStore((s) => s.workflows);

  useHotkey("mod+k", () => togglePalette(), { allowInInputs: true });
  useHotkey("/", () => setOpen(true));

  // Reset query when re-opened.
  useEffect(() => {
    if (open) setSearch("");
  }, [open]);

  function go(to: string) {
    setOpen(false);
    navigate(to);
  }

  async function engine(action: "start" | "stop" | "restart" | "reload") {
    setOpen(false);
    try {
      const t = toast.loading(`engine.${action}…`);
      const fns = api.control as Record<typeof action, () => Promise<unknown>>;
      const r = (await fns[action]()) as { status?: string };
      toast.success(`engine ${r?.status ?? action}`, { id: t });
      setTimeout(fetchAll, 600);
    } catch (e) {
      toast.error(`${action} failed: ${e instanceof Error ? e.message : "unknown"}`);
    }
  }

  async function reloadAccounts() {
    setOpen(false);
    try {
      const t = toast.loading("reloading accounts…");
      const r = await api.accounts.reload();
      toast.success(`accounts reloaded (${r.loaded})`, { id: t });
      fetchAll();
    } catch (e) {
      toast.error(`reload failed: ${e instanceof Error ? e.message : "unknown"}`);
    }
  }

  async function reapLocks() {
    setOpen(false);
    try {
      const r = await api.accounts.reapLocks();
      toast.success(`released ${r.released} locks`);
    } catch (e) {
      toast.error(`reap failed: ${e instanceof Error ? e.message : "unknown"}`);
    }
  }


  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[60] grid place-items-start pt-[12vh] px-3">
      <div
        className="absolute inset-0 bg-background/70 backdrop-blur-sm animate-in fade-in-0"
        onClick={() => setOpen(false)}
      />
      <div
        className={cn(
          "relative w-full max-w-xl rounded-xl border border-border/70 bg-popover/95 backdrop-blur-2xl shadow-2xl",
          "animate-in fade-in-0 zoom-in-95 slide-in-from-top-4 duration-200",
        )}
      >
        <Command
          label="Command Menu"
          loop
          className="flex flex-col"
          shouldFilter
        >
          <div className="flex items-center gap-2 px-3 border-b border-border/60">
            <Search className="h-4 w-4 text-muted-foreground shrink-0" />
            <Command.Input
              autoFocus
              value={search}
              onValueChange={setSearch}
              placeholder="Search pages, accounts, workflows, actions…"
              className={cn(
                "flex-1 h-12 bg-transparent text-sm outline-none",
                "placeholder:text-muted-foreground/70",
              )}
            />
            <kbd className="hidden sm:inline-flex h-6 items-center gap-1 rounded border border-border/60 bg-secondary/70 px-1.5 font-mono text-[10px] text-muted-foreground">
              esc
            </kbd>
          </div>

          <Command.List className="max-h-[60vh] overflow-y-auto p-1.5">
            <Command.Empty className="py-8 text-center text-sm text-muted-foreground">
              No matches.
            </Command.Empty>

            <Command.Group heading="Navigate">
              {NAV_CMDS.map((c) => (
                <PaletteItem
                  key={c.id}
                  value={`${c.label} ${c.keywords?.join(" ") ?? ""}`}
                  icon={c.icon}
                  onSelect={() => go(c.to)}
                >
                  {c.label}
                </PaletteItem>
              ))}
            </Command.Group>

            <Command.Group heading="Engine">
              <PaletteItem icon={Play} onSelect={() => engine("start")}>
                Start engine
              </PaletteItem>
              <PaletteItem icon={Pause} onSelect={() => engine("stop")}>
                Stop engine
              </PaletteItem>
              <PaletteItem icon={RotateCw} onSelect={() => engine("restart")}>
                Restart engine
              </PaletteItem>
              <PaletteItem icon={ChevronsUpDown} onSelect={() => engine("reload")}>
                Reload config + plugins
              </PaletteItem>
            </Command.Group>

            <Command.Group heading="Accounts">
              <PaletteItem icon={Users} onSelect={reloadAccounts}>
                Reload accounts file
              </PaletteItem>
              <PaletteItem icon={Users} onSelect={reapLocks}>
                Reap stuck account locks
              </PaletteItem>
            </Command.Group>

            {workflows.length > 0 && (
              <Command.Group heading="Recent runs">
                {workflows.slice(-8).reverse().map((w, i) => (
                  <PaletteItem
                    key={`wf-${i}`}
                    icon={Workflow}
                    onSelect={() => go("/workflows")}
                  >
                    {w.workflow ?? "workflow"} — {w.status ?? "?"}
                  </PaletteItem>
                ))}
              </Command.Group>
            )}
          </Command.List>
        </Command>
      </div>
    </div>
  );
}

interface ItemProps {
  icon: React.ComponentType<{ className?: string }>;
  onSelect: () => void;
  value?: string;
  children: React.ReactNode;
}

function PaletteItem({ icon: Icon, onSelect, value, children }: ItemProps) {
  return (
    <Command.Item
      value={value}
      onSelect={onSelect}
      className={cn(
        "flex cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-sm",
        "data-[selected=true]:bg-secondary/80 data-[selected=true]:text-foreground",
        "text-muted-foreground transition-colors",
      )}
    >
      <Icon className="h-4 w-4" />
      {children}
    </Command.Item>
  );
}
