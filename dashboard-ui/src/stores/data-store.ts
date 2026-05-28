import { create } from "zustand";

import { api, ApiError } from "@/lib/api";
import type {
  AIStatusResponse,
  AccountsListResponse,
  MetricsResponse,
  PluginRecord,
  StatusResponse,
  WorkflowSummary,
} from "@/types/api";

/**
 * The "live" data store.
 *
 * One Zustand store holds the data that a half-dozen pages all need
 * (status snapshot, metrics, workflow runs, AI status, plugins, accounts).
 * A single shared poll loop is started by `<DataPump/>` in App.tsx so we
 * don't multiply the request rate as the user navigates between pages.
 *
 * Pages can also subscribe to focused subsets through selector hooks
 * (see `useStatus`, `useMetrics`, etc. at the bottom).
 */

const RING = 60; // 60 samples == 4 minutes at 4s polling

export interface MetricSample {
  ts: number;
  cpu: number;
  memory: number;
  disk: number;
  uptime: number;
  workflowRuns: number;
  browserSessions: number;
}

interface DataState {
  // --- snapshots ---
  status: StatusResponse | null;
  metrics: MetricsResponse | null;
  ai: AIStatusResponse | null;
  workflows: WorkflowSummary[];
  plugins: PluginRecord[];
  accounts: AccountsListResponse | null;

  // --- timeseries ring buffer ---
  samples: MetricSample[];

  // --- meta ---
  lastFetchedAt: number | null;
  lastError: string | null;
  loading: boolean;
  online: boolean;

  // --- actions ---
  fetchAll: () => Promise<void>;
  refreshAccounts: () => Promise<void>;
  refreshPlugins: () => Promise<void>;
  setError: (e: string | null) => void;
}

export const useDataStore = create<DataState>((set, get) => ({
  status: null,
  metrics: null,
  ai: null,
  workflows: [],
  plugins: [],
  accounts: null,
  samples: [],
  lastFetchedAt: null,
  lastError: null,
  loading: true,
  online: false,

  setError: (e) => set({ lastError: e }),

  fetchAll: async () => {
    try {
      // The five core endpoints power roughly every page. Fire them in
      // parallel and tolerate individual failures (e.g., AI not configured).
      const [status, metrics, ai, recent, plugins] = await Promise.all([
        api.status().catch(() => null),
        api.metrics().catch(() => null),
        api.ai.status().catch(() => null),
        api.workflows.recent(50).catch(() => ({ results: [] })),
        api.plugins.list().catch(() => ({ plugins: [] })),
      ]);

      const sample: MetricSample | null = metrics
        ? {
            ts: Date.now(),
            cpu: metrics.system.cpu_percent ?? 0,
            memory: metrics.system.memory_percent ?? 0,
            disk: metrics.system.disk_percent ?? 0,
            uptime: metrics.system.uptime_seconds ?? 0,
            workflowRuns: metrics.workflow_runs_recent ?? 0,
            browserSessions: metrics.browser_sessions ?? 0,
          }
        : null;

      set((s) => ({
        status,
        metrics,
        ai,
        workflows: recent.results ?? [],
        plugins: plugins.plugins ?? [],
        samples: sample ? [...s.samples.slice(-(RING - 1)), sample] : s.samples,
        lastFetchedAt: Date.now(),
        loading: false,
        online: status !== null || metrics !== null,
        lastError: null,
      }));
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.status} ${err.statusText}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      set({ loading: false, online: false, lastError: msg });
    }
  },

  refreshAccounts: async () => {
    try {
      const accounts = await api.accounts.list();
      set({ accounts });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "accounts fetch failed";
      set({ lastError: msg });
    }
  },

  refreshPlugins: async () => {
    try {
      const r = await api.plugins.list();
      set({ plugins: r.plugins });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "plugins fetch failed";
      set({ lastError: msg });
    }
  },
}));

/* ------------------------------------------------------------------ *
 * Convenience selectors (cheaper than `useDataStore((s) => ...)` at
 * call sites because they're stable references).
 * ------------------------------------------------------------------ */

export const useStatus = () => useDataStore((s) => s.status);
export const useMetrics = () => useDataStore((s) => s.metrics);
export const useAi = () => useDataStore((s) => s.ai);
export const useWorkflows = () => useDataStore((s) => s.workflows);
export const usePlugins = () => useDataStore((s) => s.plugins);
export const useAccounts = () => useDataStore((s) => s.accounts);
export const useSamples = () => useDataStore((s) => s.samples);
export const useLastFetched = () => useDataStore((s) => s.lastFetchedAt);
export const useOnline = () => useDataStore((s) => s.online);
export const useGlobalLoading = () => useDataStore((s) => s.loading);
