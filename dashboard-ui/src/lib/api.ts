import type {
  AIStatusResponse,
  AccountRow,
  AccountsListResponse,
  AccountsStatusResponse,
  AuditEntry,
  ConfigResponse,
  IntentRow,
  LogChannel,
  LogFile,
  LogTail,
  MemoryPage,
  MemorySelector,
  MetricsResponse,
  PluginRecord,
  StatusResponse,
  TaskRow,
  WorkflowSummary,
} from "@/types/api";

const TOKEN_KEY = "automation.api.token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(value: string): void {
  if (value) localStorage.setItem(TOKEN_KEY, value);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public statusText: string,
    public body?: unknown,
  ) {
    super(`${status} ${statusText}`);
  }
}

interface RequestOpts extends Omit<RequestInit, "body"> {
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: RequestOpts["query"]): string {
  // The backend lives at the same origin as the dashboard (FastAPI mounts
  // /dashboard alongside /status, /accounts, ...). We send absolute-from-root
  // paths so the dashboard works behind nginx and through SSH tunnels.
  const base = path.startsWith("/") ? path : `/${path}`;
  if (!query) return base;
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    usp.set(k, String(v));
  }
  const qs = usp.toString();
  return qs ? `${base}?${qs}` : base;
}

export async function request<T = unknown>(
  path: string,
  opts: RequestOpts = {},
): Promise<T> {
  const headers = new Headers(opts.headers);
  headers.set("Accept", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (opts.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(buildUrl(path, opts.query), {
    ...opts,
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal,
  });

  if (!res.ok) {
    let body: unknown = undefined;
    try {
      body = await res.json();
    } catch {
      try {
        body = await res.text();
      } catch {
        /* empty */
      }
    }
    throw new ApiError(res.status, res.statusText, body);
  }

  const ct = res.headers.get("Content-Type") ?? "";
  if (ct.includes("application/json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

/** ------------------------------------------------------------------- *
 * Typed endpoint helpers — one function per backend route.
 * Keep this file as the single source of truth for the API surface.
 * ------------------------------------------------------------------- */

export const api = {
  health: () => request<{ status: string }>("/health"),

  status: (signal?: AbortSignal) =>
    request<StatusResponse>("/status", { signal }),

  metrics: (signal?: AbortSignal) =>
    request<MetricsResponse>("/metrics", { signal }),

  prometheusMetrics: (signal?: AbortSignal) =>
    request<string>("/metrics/prometheus", { signal }),

  control: {
    start: () => request<{ status: string }>("/control/start", { method: "POST" }),
    stop: () => request<{ status: string }>("/control/stop", { method: "POST" }),
    restart: () =>
      request<{ status: string }>("/control/restart", { method: "POST" }),
    reload: () =>
      request<{ status: string }>("/control/reload", { method: "POST" }),
    audit: () =>
      request<{ entries: AuditEntry[] }>("/control/audit"),
  },

  plugins: {
    list: () => request<{ plugins: PluginRecord[] }>("/plugins"),
    enable: (name: string) =>
      request<{ name: string; enabled: boolean }>(
        `/plugins/${encodeURIComponent(name)}/enable`,
        { method: "POST" },
      ),
    disable: (name: string) =>
      request<{ name: string; enabled: boolean }>(
        `/plugins/${encodeURIComponent(name)}/disable`,
        { method: "POST" },
      ),
    restart: (name: string) =>
      request<{ name: string; restarted: boolean }>(
        `/plugins/${encodeURIComponent(name)}/restart`,
        { method: "POST" },
      ),
    reloadAll: () =>
      request<{ reloaded: boolean }>("/plugins/reload", { method: "POST" }),
  },

  config: {
    get: () => request<ConfigResponse>("/config"),
    reload: () =>
      request<{ reloaded: boolean }>("/config/reload", { method: "POST" }),
  },

  tasks: {
    list: (status?: string) =>
      request<{ tasks: TaskRow[] }>("/tasks", {
        query: { status_filter: status },
      }),
    get: (id: string) =>
      request<TaskRow & { result?: string | null; metadata?: unknown }>(
        `/tasks/${encodeURIComponent(id)}`,
      ),
    cancel: (id: string) =>
      request<{ cancelled: boolean }>(
        `/tasks/${encodeURIComponent(id)}/cancel`,
        { method: "POST" },
      ),
  },

  logs: {
    list: () =>
      request<{ directory: string; files: LogFile[] }>("/logs"),
    tail: (name: LogChannel, lines = 200, signal?: AbortSignal) =>
      request<LogTail>(`/logs/${name}`, { query: { lines }, signal }),
  },

  accounts: {
    list: (filter?: string, limit = 200) =>
      request<AccountsListResponse>("/accounts", {
        query: { status_filter: filter, limit },
      }),
    status: () => request<AccountsStatusResponse>("/accounts/status"),
    completed: (limit = 200) =>
      request<{ count: number; accounts: AccountRow[] }>("/accounts/completed", {
        query: { limit },
      }),
    failed: (limit = 200) =>
      request<{ count: number; accounts: AccountRow[] }>("/accounts/failed", {
        query: { limit },
      }),
    rejected: (limit = 200) =>
      request<{ rejected: Array<Record<string, unknown>> }>(
        "/accounts/rejected",
        { query: { limit } },
      ),
    results: (account_id?: string, limit = 100) =>
      request<{ results: Array<Record<string, unknown>> }>("/accounts/results", {
        query: { account_id, limit },
      }),
    reload: () =>
      request<{ loaded: number; rejected: number; stats: unknown }>(
        "/accounts/reload",
        { method: "POST" },
      ),
    reapLocks: () =>
      request<{ released: number }>("/accounts/locks/reap", { method: "POST" }),
    get: (id: string) =>
      request<{ account: AccountRow }>(
        `/accounts/${encodeURIComponent(id)}`,
      ),
    reset: (id: string) =>
      request<{ reset: boolean }>(
        `/accounts/${encodeURIComponent(id)}/reset`,
        { method: "POST" },
      ),
    pause: (id: string) =>
      request<{ paused: boolean }>(
        `/accounts/${encodeURIComponent(id)}/pause`,
        { method: "POST" },
      ),
    resume: (id: string) =>
      request<{ resumed: boolean }>(
        `/accounts/${encodeURIComponent(id)}/resume`,
        { method: "POST" },
      ),
    release: (id: string) =>
      request<{ released: boolean }>(
        `/accounts/${encodeURIComponent(id)}/release`,
        { method: "POST" },
      ),
  },

  workflows: {
    list: () =>
      request<{ directory: string; workflows: string[] }>("/workflows"),
    get: (name: string) =>
      request<{ workflow: Record<string, unknown> }>(
        `/workflows/${encodeURIComponent(name)}`,
      ),
    run: (name: string, body: { account_id?: string; inputs?: unknown }) =>
      request<{ status: string; workflow: string; account_id?: string }>(
        `/workflows/${encodeURIComponent(name)}/run`,
        { method: "POST", body },
      ),
    runForAccounts: (
      name: string,
      body: {
        account_ids: string[];
        inputs?: unknown;
        parallel?: boolean;
        max_parallel?: number;
        stop_on_failure?: boolean;
      },
    ) =>
      request<{
        status: string;
        workflow: string;
        accounts: string[];
        parallel: boolean;
      }>(`/workflows/${encodeURIComponent(name)}/run_for_accounts`, {
        method: "POST",
        body,
      }),
    runInline: (body: {
      name?: string;
      description?: string;
      inputs?: unknown;
      account_id?: string;
      steps: Array<Record<string, unknown>>;
    }) =>
      request<{ status: string; workflow: string }>(
        "/workflows/run_inline",
        { method: "POST", body },
      ),
    recent: (limit = 50) =>
      request<{ results: WorkflowSummary[] }>(
        "/workflows/results/recent",
        { query: { limit } },
      ),
  },

  ai: {
    status: () => request<AIStatusResponse>("/ai/status"),
    intents: () => request<{ intents: IntentRow[] }>("/ai/intents"),
    pages: (limit = 50) =>
      request<{ pages: MemoryPage[] }>("/ai/memory/pages", { query: { limit } }),
    stats: () =>
      request<{ stats: Record<string, unknown> }>("/ai/memory/stats"),
    selectors: (page_signature: string, intent: string) =>
      request<{ selectors: MemorySelector[] }>("/ai/memory/selectors", {
        query: { page_signature, intent },
      }),
  },

  distributed: {
    status: () => request<Record<string, unknown>>("/distributed/status"),
  },
};
