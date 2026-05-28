/**
 * Type definitions mirroring the FastAPI backend's response shapes.
 *
 * These are intentionally permissive (`Record<string, unknown>` for nested
 * payloads we don't fully consume) so the dashboard never breaks when the
 * backend adds new fields.
 */

export interface PluginRecord {
  name: string;
  enabled: boolean;
  started: boolean;
  version?: string;
  module_path?: string;
  metadata?: Record<string, unknown>;
  error?: string | null;
}

export interface AccountsProgress {
  total: number;
  pending: number;
  running: number;
  completed: number;
  failed: number;
  skipped?: number;
  rejected?: number;
  speed_per_minute?: number;
  completion_rate?: number;
  failure_rate?: number;
  last_loaded_at?: number;
}

export interface SchedulerInfo {
  max_workers: number;
  jobs: string[];
}

export interface StatusResponse {
  running: boolean;
  uptime_seconds: number;
  components: Record<string, unknown>;
  status: "ok" | "warn" | "error" | string;
  plugins: PluginRecord[];
  scheduler: SchedulerInfo;
  queues: Record<string, number>;
  accounts: AccountsProgress | null;
  browser_sessions: string[];
  ai_enabled: boolean;
}

export interface SystemMetrics {
  cpu_percent: number;
  memory_percent: number;
  memory_used_mb: number;
  disk_percent: number;
  uptime_seconds: number;
  status: string;
}

export interface MetricsResponse {
  system: SystemMetrics;
  tasks: { by_status: Record<string, number>; total: number };
  accounts: AccountsProgress | Record<string, never>;
  queues: Record<string, number>;
  browser_sessions: number;
  workflow_runs_recent: number;
}

export interface AccountRow {
  id: string;
  number?: number | string;
  username?: string;
  email?: string;
  status: string;
  attempts?: number;
  last_error?: string | null;
  last_workflow?: string | null;
  metadata?: Record<string, unknown>;
  locked_by?: string | null;
  lease_expires_at?: number | null;
  updated_at?: number;
}

export interface AccountsListResponse {
  stats: AccountsProgress | Record<string, never>;
  accounts: AccountRow[];
}

export interface AccountsStatusResponse {
  configured: boolean;
  source_file?: string;
  last_loaded_at?: number;
  last_load_count?: number;
  rejected_count?: number;
  lease_seconds?: number;
  max_attempts?: number;
  progress?: AccountsProgress;
}

export interface WorkflowSummary {
  workflow?: string;
  account_id?: string | null;
  status?: string;
  duration_ms?: number;
  steps?: number;
  started_at?: number;
  ended_at?: number;
  records?: Array<Record<string, unknown>>;
  profile_id?: string | null;
  proxy?: string | null;
  // batch-only:
  kind?: "batch";
  total?: number;
  succeeded?: number;
  failed?: number;
  accounts?: Array<{
    account_id?: string | null;
    status?: string;
    profile_id?: string | null;
    proxy?: string | null;
    duration_ms?: number;
    error?: string | null;
  }>;
  error?: string | null;
}

export interface AIDecision {
  goal?: string;
  success?: boolean;
  plan?: { steps?: Array<Record<string, unknown>> };
  page_signature?: string;
  intent?: string;
  reasoning?: string;
  ts?: number;
}

export interface AIStatusResponse {
  enabled: boolean;
  dry_run?: boolean;
  memory?: boolean;
  last_decision?: AIDecision | null;
}

export interface IntentRow {
  name: string;
  description: string;
  keywords: string[];
  roles: string[];
}

export interface MemoryPage {
  signature?: string;
  url?: string;
  title?: string;
  count?: number;
  last_seen?: number;
  [k: string]: unknown;
}

export interface MemorySelector {
  selector: string;
  strategy: string;
  confidence: number;
  success_count: number;
  fail_count: number;
}

export interface TaskRow {
  id: string;
  name: string;
  status: string;
  attempts: number;
  error?: string | null;
  started_at?: number | null;
  completed_at?: number | null;
}

export interface LogFile {
  name: string;
  size: number;
  mtime: number;
}

export interface LogTail {
  name: string;
  lines: string[];
}

export interface ConfigResponse {
  config: Record<string, unknown>;
}

export type LogChannel = "activity" | "error" | "debug";

export interface AuditEntry {
  ts: number;
  who: string;
  action: string;
  detail?: string;
}
