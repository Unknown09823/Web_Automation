import { lazy, Suspense, useEffect } from "react";
import { Route, Routes } from "react-router-dom";
import { Toaster } from "sonner";
import { motion } from "framer-motion";

import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";
import { CommandPalette } from "@/components/command-palette";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Skeleton } from "@/components/ui/skeleton";
import { useDataStore } from "@/stores/data-store";
import { useUIStore } from "@/stores/ui-store";

const Overview = lazy(() => import("@/pages/overview"));
const Accounts = lazy(() => import("@/pages/accounts"));
const Workflows = lazy(() => import("@/pages/workflows"));
const AICenter = lazy(() => import("@/pages/ai-center"));
const LiveBrowser = lazy(() => import("@/pages/live-browser"));
const Plugins = lazy(() => import("@/pages/plugins"));
const Logs = lazy(() => import("@/pages/logs"));
const Analytics = lazy(() => import("@/pages/analytics"));
const Infrastructure = lazy(() => import("@/pages/infrastructure"));


/**
 * Single shared poll loop.
 * One effect drives a `setInterval` calling `fetchAll`, so as the user
 * moves between pages we don't multiply the network load.
 */
function DataPump() {
  const fetchAll = useDataStore((s) => s.fetchAll);
  const interval = useUIStore((s) => s.pollIntervalMs);
  const paused = useUIStore((s) => s.pollingPaused);

  useEffect(() => {
    fetchAll();
    if (paused) return;
    const id = setInterval(fetchAll, interval);
    return () => clearInterval(id);
  }, [fetchAll, interval, paused]);

  // Pause polling while the tab is hidden — saves resources.
  useEffect(() => {
    function onVis() {
      useUIStore.setState({ pollingPaused: document.hidden });
    }
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  return null;
}

function PageFallback() {
  return (
    <div className="space-y-4 animate-pulse">
      <Skeleton className="h-32 w-full" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-28" />
        ))}
      </div>
      <Skeleton className="h-64 w-full" />
    </div>
  );
}


export default function App() {
  return (
    <TooltipProvider delayDuration={200} skipDelayDuration={400}>
      <DataPump />
      <CommandPalette />

      <div className="min-h-screen lg:pl-60">
        <Sidebar />
        <Topbar />
        <motion.main
          key="main"
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
          className="mx-auto w-full max-w-screen-2xl px-3 sm:px-5 lg:px-8 py-5 lg:py-7"
        >
          <Suspense fallback={<PageFallback />}>
            <Routes>
              <Route path="/" element={<Overview />} />
              <Route path="/accounts" element={<Accounts />} />
              <Route path="/workflows" element={<Workflows />} />
              <Route path="/ai" element={<AICenter />} />
              <Route path="/live" element={<LiveBrowser />} />
              <Route path="/plugins" element={<Plugins />} />
              <Route path="/logs" element={<Logs />} />
              <Route path="/analytics" element={<Analytics />} />
              <Route path="/infra" element={<Infrastructure />} />
              <Route path="*" element={<Overview />} />
            </Routes>
          </Suspense>
        </motion.main>
      </div>

      <Toaster
        theme="dark"
        position="bottom-right"
        toastOptions={{
          className:
            "!bg-popover/95 !backdrop-blur-2xl !border !border-border/70 !text-foreground !rounded-md",
        }}
        closeButton
        richColors
      />
    </TooltipProvider>
  );
}
