import { NavLink } from "react-router-dom";
import { motion } from "framer-motion";
import {
  Activity,
  BarChart3,
  Boxes,
  BrainCircuit,
  ChevronRight,
  Cpu,
  FileText,
  GalleryVerticalEnd,
  LayoutGrid,
  Monitor,
  Users,
  Workflow,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { useUIStore } from "@/stores/ui-store";

interface NavItem {
  to: string;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  end?: boolean;
}

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Operate",
    items: [
      { to: "/", icon: LayoutGrid, label: "Overview", end: true },
      { to: "/accounts", icon: Users, label: "Accounts" },
      { to: "/workflows", icon: Workflow, label: "Workflows" },
      { to: "/live", icon: Monitor, label: "Live Browser" },
    ],
  },
  {
    group: "Intelligence",
    items: [
      { to: "/ai", icon: BrainCircuit, label: "AI Center" },
      { to: "/analytics", icon: BarChart3, label: "Analytics" },
    ],
  },
  {
    group: "System",
    items: [
      { to: "/plugins", icon: Boxes, label: "Plugins" },
      { to: "/logs", icon: FileText, label: "Logs" },
      { to: "/infra", icon: Cpu, label: "Infrastructure" },
    ],
  },
];


function NavRow({ item }: { item: NavItem }) {
  const setSidebarOpen = useUIStore((s) => s.setSidebarOpen);
  return (
    <NavLink
      to={item.to}
      end={item.end}
      onClick={() => setSidebarOpen(false)}
      className={({ isActive }) =>
        cn(
          "group relative flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
          "text-muted-foreground hover:text-foreground hover:bg-secondary/60",
          isActive && "text-foreground bg-secondary/70",
        )
      }
    >
      {({ isActive }) => (
        <>
          {isActive && (
            <motion.span
              layoutId="sidebar-active"
              className="absolute inset-y-1 left-0 w-0.5 rounded-full bg-primary"
              transition={{ type: "spring", bounce: 0.2, duration: 0.5 }}
            />
          )}
          <item.icon className={cn("h-4 w-4", isActive && "text-primary")} />
          <span className="flex-1 truncate">{item.label}</span>
          {isActive && (
            <ChevronRight className="h-3 w-3 opacity-60 transition-transform group-hover:translate-x-0.5" />
          )}
        </>
      )}
    </NavLink>
  );
}

export function SidebarContent() {
  return (
    <div className="flex h-full flex-col">
      <div className="px-4 pt-5 pb-3 flex items-center gap-2">
        <span className="grid place-items-center h-8 w-8 rounded-lg bg-gradient-to-br from-primary/30 to-accent/30 border border-border/60">
          <GalleryVerticalEnd className="h-4 w-4 text-primary" />
        </span>
        <div className="leading-tight">
          <div className="text-sm font-semibold tracking-tight">Automation</div>
          <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
            Control Center
          </div>
        </div>
      </div>

      <nav className="px-2 flex-1 overflow-y-auto pb-4 space-y-4">
        {NAV.map((g) => (
          <div key={g.group} className="space-y-1">
            <div className="px-2 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/80">
              {g.group}
            </div>
            <div className="space-y-0.5">
              {g.items.map((it) => (
                <NavRow key={it.to} item={it} />
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="border-t border-border/60 px-4 py-3 text-[11px] text-muted-foreground flex items-center justify-between">
        <span className="flex items-center gap-1.5">
          <Activity className="h-3 w-3" />
          v1.0.0
        </span>
        <span className="font-mono">automation-fw</span>
      </div>
    </div>
  );
}

export function Sidebar() {
  const open = useUIStore((s) => s.sidebarOpen);
  const setOpen = useUIStore((s) => s.setSidebarOpen);

  return (
    <>
      {/* Desktop */}
      <aside className="hidden lg:flex lg:fixed lg:inset-y-0 lg:left-0 lg:w-60 z-30 glass border-r border-border/60">
        <SidebarContent />
      </aside>

      {/* Mobile slide-over */}
      <div
        className={cn(
          "lg:hidden fixed inset-0 z-50 transition-opacity",
          open ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0",
        )}
      >
        <div
          className="absolute inset-0 bg-background/70 backdrop-blur-sm"
          onClick={() => setOpen(false)}
        />
        <motion.aside
          initial={false}
          animate={{ x: open ? 0 : "-100%" }}
          transition={{ type: "spring", bounce: 0.05, duration: 0.4 }}
          className="absolute inset-y-0 left-0 w-72 max-w-[85vw] glass-strong border-r border-border/70"
        >
          <SidebarContent />
        </motion.aside>
      </div>
    </>
  );
}
