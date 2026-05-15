import { getCachedSupabaseAccessToken, supabase } from "./lib/supabase";

export type WorkspaceInfo = { path: string | null; default: string | null };
export type WorkspaceProvisionInfo = { ok: boolean; path: string; created: boolean; managed: boolean };
export type HostedProject = {
  id: string;
  owner_id: string;
  name: string;
  slug: string;
  root: string;
  created_at: number;
  updated_at: number;
  archived: boolean;
};
export type ProjectTemplate = {
  id: string;
  name: string;
  category: string;
  description: string;
  best_for: string;
  tags: string[];
};
export type IdentityInfo = {
  user_id: string;
  display_name: string | null;
  email: string | null;
  has_profile: boolean;
  managed_workspace_mode: "user" | "session";
  managed_workspace_path: string;
};

export type ProviderStatus = {
  provider: ProviderChoice;
  connected: boolean;
  hint?: string | null;
  profile_id?: string | null;
  account_id?: string | null;
  auth_type?: string | null;
  project_id?: string | null;
  source?: string | null;
  recommended_model?: string | null;
  free_tier_models?: string[];
};

export type BuildMode = "full-agent" | "hybrid";

export type ProviderChoice = "nine_router" | "openai" | "anthropic" | "openrouter" | "groq" | "gemini" | "together" | "cerebras" | "xai" | "";

export type SettingsInfo = {
  default_workspace: string | null;
  llm_provider: Exclude<ProviderChoice, ""> | null;
  build_mode: BuildMode;
  nine_router_base_url: string;
  nine_router_model: string;
  openai_model: string;
  anthropic_model: string;
  openrouter_model: string;
  groq_model: string;
  gemini_model: string;
  together_model: string;
  cerebras_model: string;
  xai_model: string;
  friendly_free_tier_mode: boolean;
  agent_refinement_mode: "auto" | "off" | "always";
  agent_min_gap_seconds: number;
  nine_router_api_key_set: boolean;
  openai_api_key_set: boolean;
  anthropic_api_key_set: boolean;
  openrouter_api_key_set: boolean;
  groq_api_key_set: boolean;
  gemini_api_key_set: boolean;
  together_api_key_set: boolean;
  cerebras_api_key_set: boolean;
  xai_api_key_set: boolean;
  supabase_url: string | null;
  supabase_frontend_ready: boolean;
  supabase_anon_key_set: boolean;
  supabase_service_role_key_set: boolean;
  supabase_enabled: boolean;
  supabase_rag_status: "unconfigured" | "ready" | "missing" | "error";
  supabase_warning: string | null;
  supabase_missing_env: string[];
  providers: {
    nine_router: ProviderStatus;
    openai: ProviderStatus;
    anthropic: ProviderStatus;
    openrouter: ProviderStatus;
    groq: ProviderStatus;
    gemini: ProviderStatus;
    together: ProviderStatus;
    cerebras: ProviderStatus;
    xai: ProviderStatus;
  };
};

export type SettingsUpdate = Partial<{
  default_workspace: string | null;
  llm_provider: ProviderChoice;
  build_mode: BuildMode;
  nine_router_base_url: string;
  nine_router_model: string;
  openai_model: string;
  anthropic_model: string;
  openrouter_model: string;
  groq_model: string;
  gemini_model: string;
  together_model: string;
  cerebras_model: string;
  xai_model: string;
  friendly_free_tier_mode: boolean;
  agent_refinement_mode: "auto" | "off" | "always";
  agent_min_gap_seconds: number;
  nine_router_api_key: string | null;
  openai_api_key: string | null;
  anthropic_api_key: string | null;
  openrouter_api_key: string | null;
  groq_api_key: string | null;
  gemini_api_key: string | null;
  together_api_key: string | null;
  cerebras_api_key: string | null;
  xai_api_key: string | null;
}>;

export type UploadedImageAsset = {
  ok: boolean;
  path: string;
  name: string;
  content_type?: string | null;
  size: number;
};

export type UserPreferences = {
  profile_id: string;
  llm_provider: ProviderChoice | null;
  build_mode: BuildMode | null;
  openai_model: string | null;
  anthropic_model: string | null;
  nine_router_model: string | null;
  openrouter_model: string | null;
  groq_model: string | null;
  gemini_model: string | null;
  together_model: string | null;
  cerebras_model: string | null;
  xai_model: string | null;
};

export type ProjectPreferences = {
  project_id: string;
  build_mode: BuildMode | null;
  preview_entry: string | null;
  default_prompt_style: string | null;
};

export type ModelRouteDiagnostics = {
  ok: boolean;
  provider: string;
  model: string;
  route: string | null;
  summary: string;
  attempts: Array<{
    provider: string;
    model: string;
    source: string;
    tier: string;
    connected: boolean;
  }>;
  skipped: string[];
  metadata: Record<string, string>;
};

const envBase = (import.meta.env.VITE_API_BASE ?? "").trim().replace(/\/$/, "");
const isViteDev = Boolean(import.meta.env.DEV);
const localDevBase = typeof window !== "undefined"
  ? `${window.location.protocol}//${window.location.hostname || "localhost"}:8787`
  : "http://localhost:8787";
const hostedApiBase = "https://appora-api-production.up.railway.app";
const BASE = envBase || (isViteDev ? localDevBase : hostedApiBase);
const SESSION_STORAGE_KEY = "voiceide-session-id";
const USER_STORAGE_KEY = "voiceide-user-id";

function createSessionId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `voiceide-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
}

function getSessionId(): string {
  if (typeof window === "undefined") return "voiceide-server";
  const existing = window.localStorage.getItem(SESSION_STORAGE_KEY)?.trim();
  if (existing) return existing;
  const next = createSessionId();
  window.localStorage.setItem(SESSION_STORAGE_KEY, next);
  return next;
}

function normalizeUserId(value: string | null | undefined): string {
  const safe = String(value || "")
    .trim()
    .replace(/[^a-zA-Z0-9._:-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 120);
  return safe || "voiceide-user-default";
}

function getUserId(): string {
  if (typeof window === "undefined") return "voiceide-user-server";
  const existing = window.localStorage.getItem(USER_STORAGE_KEY)?.trim();
  if (existing) return normalizeUserId(existing);
  const next = normalizeUserId(`user-${createSessionId()}`);
  window.localStorage.setItem(USER_STORAGE_KEY, next);
  return next;
}

export function resetClientIdentity() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(SESSION_STORAGE_KEY);
  window.localStorage.removeItem(USER_STORAGE_KEY);
}

async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers ?? {});
  const sessionId = getSessionId();
  const userId = getUserId();
  headers.set("X-Appora-Session", sessionId);
  headers.set("X-Appora-User", userId);
  headers.set("X-VoiceIDE-Session", sessionId);
  headers.set("X-VoiceIDE-User", userId);

  try {
    const { data } = await supabase.auth.getSession();
    const token = data.session?.access_token?.trim() || getCachedSupabaseAccessToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
  } catch {
    const token = getCachedSupabaseAccessToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
  }

  return fetch(`${BASE}${path}`, {
    ...init,
    headers,
  });
}

export async function listHostedProjects(): Promise<{ ok: boolean; projects: HostedProject[] }> {
  const r = await apiFetch(`/api/projects`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function listProjectTemplates(): Promise<{ ok: boolean; templates: ProjectTemplate[] }> {
  const r = await apiFetch(`/api/projects/templates`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function createHostedProject(payload: { name: string; slug?: string | null; template_id?: string | null }): Promise<{ ok: boolean; project: HostedProject }> {
  const r = await apiFetch(`/api/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function renameHostedProject(projectId: string, payload: { name: string }): Promise<{ ok: boolean; project: HostedProject }> {
  const r = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function archiveHostedProject(projectId: string): Promise<{ ok: boolean; project: HostedProject }> {
  const r = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function duplicateHostedProject(projectId: string, payload: { name?: string | null } = {}): Promise<{ ok: boolean; project: HostedProject }> {
  const r = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}/duplicate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function saveHostedProjectSnapshot(projectId: string): Promise<{ ok: boolean; project: HostedProject }> {
  const r = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}/snapshot`, {
    method: "POST",
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function exportProjectZip(projectRoot: string): Promise<{ blob: Blob; filename: string }> {
  const r = await apiFetch(`/api/projects/export?project_root=${encodeURIComponent(projectRoot)}`);
  if (!r.ok) throw new Error(await r.text());
  const disposition = r.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="?([^";]+)"?/i);
  const fallback = `${(projectRoot || "appora-project").split("/").filter(Boolean).pop() || "appora-project"}.zip`;
  return { blob: await r.blob(), filename: match?.[1] || fallback };
}

export async function getWorkspace(): Promise<WorkspaceInfo> {
  const r = await apiFetch(`/api/workspace`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function setWorkspace(path: string): Promise<{ ok: boolean; path: string }> {
  const r = await apiFetch(`/api/workspace`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function pickWorkspaceNative(): Promise<{ ok: boolean; path: string | null }> {
  const r = await apiFetch(`/api/workspace/pick`, {
    method: "POST",
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

const IMPORT_IGNORED_SEGMENTS = new Set([
  "node_modules",
  ".git",
  "dist",
  "build",
  ".next",
  ".nuxt",
  ".output",
  ".turbo",
  ".cache",
  "coverage",
]);
const IMPORT_MAX_BATCH_BYTES = 3_000_000;
const IMPORT_MAX_BATCH_FILES = 100;

function shouldSkipImportedPath(rel: string) {
  return rel.split("/").some((part) => IMPORT_IGNORED_SEGMENTS.has(part));
}

export async function importBrowserFolder(files: File[]): Promise<WorkspaceProvisionInfo> {
  const entries = files
    .map((file) => ({
      file,
      rel: ((file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name).replace(/^\/+/, ""),
    }))
    .filter(({ rel }) => rel && !shouldSkipImportedPath(rel));

  if (entries.length === 0) {
    throw new Error("Folder ini kosong, atau isinya cuma folder generated seperti node_modules/dist.");
  }

  const oversized = entries.find(({ file }) => file.size > IMPORT_MAX_BATCH_BYTES);
  if (oversized) {
    throw new Error(`File terlalu besar untuk import browser deployment ini: ${oversized.rel}`);
  }

  let lastResponse: WorkspaceProvisionInfo | null = null;
  let batch: typeof entries = [];
  let batchBytes = 0;

  const flushBatch = async () => {
    if (batch.length === 0) return;
    const form = new FormData();
    for (const entry of batch) {
      form.append("files", entry.file, entry.file.name);
      form.append("paths", entry.rel);
    }
    const r = await apiFetch(`/api/workspace/import-browser-folder`, {
      method: "POST",
      body: form,
    });
    if (!r.ok) throw new Error(await r.text());
    lastResponse = await r.json();
    batch = [];
    batchBytes = 0;
  };

  for (const entry of entries) {
    const nextBytes = batchBytes + entry.file.size;
    if (batch.length >= IMPORT_MAX_BATCH_FILES || (batch.length > 0 && nextBytes > IMPORT_MAX_BATCH_BYTES)) {
      await flushBatch();
    }
    batch.push(entry);
    batchBytes += entry.file.size;
  }

  await flushBatch();

  if (!lastResponse) {
    throw new Error("Tidak ada file yang berhasil diimport.");
  }
  return lastResponse;
}

export async function provisionWorkspace(): Promise<WorkspaceProvisionInfo> {
  const r = await apiFetch(`/api/workspace/provision`, {
    method: "POST",
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getIdentity(): Promise<IdentityInfo> {
  const r = await apiFetch(`/api/identity`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getUserPreferences(): Promise<{ ok: boolean; preferences: UserPreferences }> {
  const r = await apiFetch(`/api/preferences/user`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function updateUserPreferences(payload: Partial<UserPreferences>): Promise<{ ok: boolean; preferences: UserPreferences }> {
  const r = await apiFetch(`/api/preferences/user`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getProjectPreferences(projectId: string): Promise<{ ok: boolean; preferences: ProjectPreferences }> {
  const r = await apiFetch(`/api/preferences/projects/${encodeURIComponent(projectId)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function updateProjectPreferences(projectId: string, payload: Partial<ProjectPreferences>): Promise<{ ok: boolean; preferences: ProjectPreferences }> {
  const r = await apiFetch(`/api/preferences/projects/${encodeURIComponent(projectId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function listDir(path: string): Promise<{ items: Array<{ name: string; path: string; type: "dir" | "file" }> }> {
  const r = await apiFetch(`/api/fs/list`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function readFile(path: string): Promise<{ content: string }> {
  const r = await apiFetch(`/api/fs/read`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function writeFile(path: string, content: string): Promise<{ ok: boolean }> {
  const r = await apiFetch(`/api/fs/write`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getSettings(): Promise<SettingsInfo> {
  const r = await apiFetch(`/api/settings`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getModels(provider: string): Promise<{ provider: string; models: string[] }> {
  const r = await apiFetch(`/api/models?provider=${encodeURIComponent(provider)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getModelRouteDiagnostics(provider: string, model: string): Promise<ModelRouteDiagnostics> {
  const qs = new URLSearchParams({ provider, model });
  const r = await apiFetch(`/api/model-routes/diagnose?${qs.toString()}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function updateSettings(patch: SettingsUpdate): Promise<{ ok: boolean; changed: string[] }> {
  const r = await apiFetch(`/api/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function applyMany(
  ops: Array<{ path: string; content: string; expected_sha256?: string | null; expected_exists?: boolean | null }>,
  overwrite = false
): Promise<{ ok: boolean; count: number }> {
  const r = await apiFetch(`/api/fs/apply_many`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ops, overwrite }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type CheckpointItem = { path: string; name: string; updated_at: number };

export async function listCheckpoints(projectRoot = "."): Promise<{ ok: boolean; items: CheckpointItem[] }> {
  const r = await apiFetch(`/api/checkpoints?project_root=${encodeURIComponent(projectRoot)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function restoreCheckpoint(path: string): Promise<{ ok: boolean; restored: number; skipped: number }> {
  const r = await apiFetch(`/api/checkpoints/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function detectProjects(): Promise<{ ok: boolean; projects: Array<{ root: string; name: string; has_dev: boolean }> }> {
  const r = await apiFetch(`/api/run/detect`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function runStart(project_root: string, port?: number): Promise<{ ok: boolean; id: string; pid: number; url: string; direct_url?: string; project_root: string }> {
  const r = await apiFetch(`/api/run/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_root, port }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function runList(): Promise<{ ok: boolean; items: Array<{ id: string; project_root: string; port: number; url: string; pid: number | null; running: boolean }> }> {
  const r = await apiFetch(`/api/run/list`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function runLogs(id: string, limit = 300): Promise<{ ok: boolean; id: string; pid: number | null; running: boolean; logs: string[] }> {
  const r = await apiFetch(`/api/run/logs?id=${encodeURIComponent(id)}&limit=${limit}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function runStop(id: string): Promise<{ ok: boolean }> {
  const r = await apiFetch(`/api/run/stop?id=${encodeURIComponent(id)}`, { method: "POST" });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function runClose(id: string): Promise<{ ok: boolean }> {
  const r = await apiFetch(`/api/run/close?id=${encodeURIComponent(id)}`, { method: "POST" });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type PreviewAuditResult = {
  ok: boolean;
  preview_url: string;
  audit_mode: "browser" | "html";
  title: string;
  meta_description: string;
  headings: string[];
  subheadings: string[];
  buttons: string[];
  links: string[];
  form_count: number;
  input_count: number;
  interactive_count?: number;
  word_count: number;
  image_count: number;
  images_missing_alt: number;
  broken_images?: string[];
  unlabeled_interactive?: string[];
  small_tap_targets?: string[];
  text_overflow_nodes?: string[];
  mobile_text_overflow_nodes?: string[];
  fixed_overlays?: string[];
  mobile_fixed_overlays?: string[];
  viewport?: { width?: number; height?: number };
  mobile_viewport?: { width?: number; height?: number };
  console_errors: string[];
  page_errors: string[];
  runtime_warnings: string[];
  issues: string[];
  quality_checks?: Array<{
    id: string;
    label: string;
    ok: boolean;
    detail: string;
  }>;
  excerpt: string;
  summary: string;
};

export async function auditPreview(preview_url: string, project_root = ".", attempts = 3, mode: "auto" | "html" | "browser" = "auto"): Promise<PreviewAuditResult> {
  const r = await apiFetch(`/api/preview/audit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preview_url, project_root, attempts, mode }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type ProjectValidationRun = {
  ok: boolean;
  project_root: string;
  commands: string[];
  results: Array<{ command: string; ok: boolean; stdout: string; stderr: string; returncode: number }>;
  ran: number;
  passed: number;
  failed: number;
};

export async function validateProject(project_root: string, max_commands = 4): Promise<ProjectValidationRun> {
  const r = await apiFetch(`/api/project/validate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_root, max_commands }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type TerminalRunResult = {
  ok: boolean;
  stdout: string;
  stderr: string;
  returncode: number;
  synced_files?: number;
};

export async function terminalRun(command: string, cwd?: string): Promise<TerminalRunResult> {
  const r = await apiFetch(`/api/terminal/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, cwd }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function uploadImageAsset(project_root: string, file: File): Promise<UploadedImageAsset> {
  const form = new FormData();
  form.append("project_root", project_root);
  form.append("file", file, file.name);
  const r = await apiFetch(`/api/assets/image`, {
    method: "POST",
    body: form,
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export type AgentChange = {
  path: string;
  new_content: string;
  diff: string;
  old_sha256?: string;
  new_sha256?: string;
  old_exists?: boolean;
};
export type AgentIntent = {
  kind: "command" | "conversation" | "mixed" | "inspection";
  confidence: number;
  rationale: string;
  should_write_files: boolean;
  should_run_tools: boolean;
  wants_app_builder: boolean;
};
export type AgentRunTrace = {
  passes: number;
  memory_hits: Array<{
    kind: string;
    source: string;
    title: string;
    score: number;
    text: string;
  }>;
  skills: Array<{
    skill_id: string;
    title: string;
    source: string;
  }>;
  mcp_servers: Array<{
    name: string;
    transport: string;
    target: string;
    tools: string[];
    source: string;
  }>;
  mcp_tools_used: Array<{
    server: string;
    tool: string;
    ok: boolean;
    duration_ms: number;
    error?: string | null;
    arguments?: Record<string, unknown>;
    text?: string;
  }>;
  plan?: Array<{
    stage: string;
    title: string;
    detail: string;
    files?: string[];
  }>;
  verification?: Array<{
    name: string;
    ok: boolean;
    detail: string;
  }>;
  warnings?: Array<{
    phase: string;
    message: string;
  }>;
};
export type AgentResult = {
  job_id?: string | null;
  spoken: string;
  log: string;
  changes: AgentChange[];
  actions: Array<{ type: string; [key: string]: unknown }>;
  intent?: AgentIntent;
  trace?: AgentRunTrace;
};
export type AgentJob = {
  id: string;
  owner_id: string;
  project_root: string;
  build_mode?: BuildMode | string | null;
  status: "queued" | "running" | "completed" | "failed" | "cancelled" | string;
  input: string;
  result?: AgentResult | Record<string, unknown> | null;
  error?: string | null;
  created_at?: string | number | null;
  updated_at?: string | number | null;
  started_at?: string | number | null;
  completed_at?: string | number | null;
};
export type AgentJobEvent = {
  id: number;
  job_id: string;
  event_type: AgentStreamEvent["event"] | string;
  payload: Record<string, unknown>;
  created_at?: string | number | null;
};
export type AgentCapabilities = {
  ok: boolean;
  runtime: string;
  supports: {
    graph_runtime: boolean;
    short_term_memory_rag: boolean;
    project_scoped_short_memory?: boolean;
    long_term_memory_rag: boolean;
    vector_memory_retrieval?: boolean;
    skill_registry: boolean;
    mcp_registry: boolean;
    mcp_tool_execution: boolean;
    autonomous_mcp_loop?: boolean;
    deep_work_preflight?: boolean;
    repo_symbol_tools?: boolean;
    route_analysis_tool?: boolean;
    quality_scan_tool?: boolean;
    interaction_intent_detection?: boolean;
    command_conversation_boundary?: boolean;
    read_only_inspection_boundary?: boolean;
    supabase_memory_backend?: boolean;
    supabase_rag_ready?: boolean;
    component_library_awareness?: boolean;
    headless_browser_runtime?: boolean;
    playwright_preview_audit?: boolean;
    webcontainer_runtime?: boolean;
    browser_dom_audit?: boolean;
    preview_quality_checks?: boolean;
    preview_audit_mode?: "browser" | "html";
    provider_fallback_routing?: boolean;
    tool_actions: string[];
    streaming_transport: boolean;
    native_provider_token_streaming: boolean;
  };
  boundaries: {
    project_root: string;
    memory_store: string;
    custom_skills_dir: string[];
    mcp_config_candidates: string[];
    supabase_rag_table?: string | null;
    mcp_loop_budget?: number;
    local_tool_names?: string[];
  };
  memory: {
    session_entries: number;
    project_entries: number;
    latest_session_ts: number | null;
    latest_project_ts: number | null;
    has_project_profile?: boolean;
    project_profile_updated_at?: number | null;
    retrieval_backend?: string;
    supabase_rag_status?: "ready" | "missing" | "error" | "unconfigured";
    supabase_warning?: string | null;
  };
  stack: {
    component_libraries: string[];
    headless_browser: boolean;
    playwright: boolean;
    webcontainer: boolean;
    node_runtime: boolean;
    preview_audit_mode: "browser" | "html";
  };
  discovered_mcp_servers: Array<{
    name: string;
    transport: string;
    target: string;
    tools: string[];
    source: string;
    live_tools?: Array<{
      name: string;
      description: string;
      input_schema: Record<string, unknown>;
    }>;
  }>;
  local_tools?: Array<{
    name: string;
    description: string;
    input_schema: Record<string, unknown>;
  }>;
};
export type AgentStreamEvent = {
  event: "status" | "delta" | "done" | "error";
  data: Record<string, unknown>;
};

export async function fetchAgentCapabilities(project_root: string, includeLiveTools = false): Promise<AgentCapabilities> {
  const params = new URLSearchParams({ project_root });
  if (includeLiveTools) params.set("include_live_tools", "true");
  const r = await apiFetch(`/api/agent/capabilities?${params.toString()}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getAgentJob(jobId: string): Promise<{ ok: boolean; job: AgentJob; source: "supabase" | "session" }> {
  const r = await apiFetch(`/api/agent/jobs/${encodeURIComponent(jobId)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getAgentJobEvents(jobId: string, afterId = 0): Promise<{ ok: boolean; events: AgentJobEvent[]; source: "supabase" | "session" }> {
  const params = new URLSearchParams({ after_id: String(Math.max(0, afterId)) });
  const r = await apiFetch(`/api/agent/jobs/${encodeURIComponent(jobId)}/events?${params.toString()}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function parseSseChunk(chunk: string): AgentStreamEvent[] {
  const out: AgentStreamEvent[] = [];
  const messages = chunk.split("\n\n");
  for (const rawMessage of messages) {
    const message = rawMessage.trim();
    if (!message) continue;
    let eventName = "status";
    const dataLines: string[] = [];
    for (const line of message.split("\n")) {
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) continue;
    try {
      out.push({
        event: eventName as AgentStreamEvent["event"],
        data: JSON.parse(dataLines.join("\n")) as Record<string, unknown>,
      });
    } catch {
      out.push({ event: "error", data: { message: "Invalid stream payload" } });
    }
  }
  return out;
}

export async function streamAgent(
  input: string,
  onEvent: (event: AgentStreamEvent) => void,
  active_file?: string | null,
  selection?: string | null,
  project_root?: string | null,
  build_mode?: BuildMode,
  asset_paths?: string[],
  current_content?: string | null,
  open_files?: string[],
  preview_url?: string | null,
  editor_status?: string | null,
): Promise<AgentResult> {
  const r = await apiFetch(`/api/agent`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({
      input,
      mode: "type",
      active_file,
      selection,
      current_content,
      open_files,
      project_root,
      build_mode,
      preview_url,
      editor_status,
      asset_paths,
      stream: true,
    }),
  });
  if (!r.ok) throw new Error(await r.text());
  if (!r.body) throw new Error("Streaming response body is missing");

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: AgentResult | null = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      for (const event of parseSseChunk(part + "\n\n")) {
        onEvent(event);
        if (event.event === "done" && event.data.result) {
          finalResult = event.data.result as AgentResult;
        }
        if (event.event === "error") {
          const message = typeof event.data.message === "string" ? event.data.message : "Agent stream failed";
          throw new Error(message);
        }
      }
    }
  }

  if (buffer.trim()) {
    for (const event of parseSseChunk(buffer)) {
      onEvent(event);
      if (event.event === "done" && event.data.result) {
        finalResult = event.data.result as AgentResult;
      }
    }
  }

  if (!finalResult) throw new Error("Agent stream ended before delivering a final result");
  return finalResult;
}

export async function agent(
  input: string,
  active_file?: string | null,
  selection?: string | null,
  project_root?: string | null,
  build_mode?: BuildMode,
  asset_paths?: string[],
  current_content?: string | null,
  open_files?: string[],
  preview_url?: string | null,
  editor_status?: string | null,
): Promise<AgentResult> {
  const r = await apiFetch(`/api/agent`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      input,
      mode: "type",
      active_file,
      selection,
      current_content,
      open_files,
      project_root,
      build_mode,
      preview_url,
      editor_status,
      asset_paths,
    }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
