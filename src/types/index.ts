export type ExplorerItem = { name: string; path: string; type: "dir" | "file" };
export type FileBuffer = { content: string; dirty: boolean };
export type IconProps = { className?: string; size?: number | string };
export type AgentStep = { id: string; icon: string; text: string };
export type ProjectInfo = { root: string; name: string; has_dev: boolean };
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
export type RunInfo = { id: string; project_root: string; url: string; running: boolean };
export type AgentAction = { type: string; [key: string]: unknown };
export type AgentLiveItem = {
  id: string;
  role: "user" | "assistant" | "tool";
  text: string;
  tone?: "default" | "working" | "success" | "error";
  meta?: string | null;
};
export type AgentAuditSnapshot = {
  id: string;
  label: string;
  passes: number;
  contextFiles?: string[];
  finalConfidence?: string;
  memoryHits: Array<{
    kind: string;
    source: string;
    title: string;
    score: number;
    text: string;
  }>;
  skills: Array<{
    skillId: string;
    title: string;
    source: string;
  }>;
  mcpServers: Array<{
    name: string;
    transport: string;
    target: string;
    tools: string[];
    source: string;
  }>;
  mcpToolsUsed: Array<{
    server: string;
    tool: string;
    ok: boolean;
    durationMs: number;
    error?: string | null;
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
  validationRuns?: Array<{
    label: string;
    ok: boolean;
    ran: number;
    failed: number;
    commands: string[];
  }>;
  previewAudits?: Array<{
    label: string;
    ok: boolean;
    auditMode: string;
    blocking: number;
    warnings: number;
    summary: string;
  }>;
  repairPasses?: Array<{
    label: string;
    producedChanges: number;
    producedActions: number;
    verifierFailures: number;
  }>;
  commandPolicyDecisions?: Array<{
    command: string;
    riskLevel: string;
    ok: boolean;
    reason: string;
  }>;
};
export type WorkspaceInfo = { path: string | null; default: string | null };
export type WorkspaceProvisionInfo = { ok: boolean; path: string; created: boolean; managed: boolean };
export type IdentityInfo = {
  user_id: string;
  display_name: string | null;
  email: string | null;
  has_profile: boolean;
  managed_workspace_mode: "user" | "session";
  managed_workspace_path: string;
};
export type GoogleUserInfo = {
  sub: string;
  email: string | null;
  name: string | null;
  picture: string | null;
};
export type GoogleAuthStatus = {
  ok: boolean;
  authenticated: boolean;
  phase: string;
  auth_url?: string | null;
  user?: GoogleUserInfo | null;
};
export type ProviderChoice = "nine_router" | "openai" | "anthropic" | "openrouter" | "groq" | "gemini" | "together" | "cerebras" | "xai" | "";
export type BuildMode = "full-agent" | "hybrid";
export type ProviderStatus = {
  provider: ProviderChoice;
  connected: boolean;
  hint?: string | null;
  profile_id?: string | null;
  account_id?: string | null;
  auth_type?: string | null;
  project_id?: string | null;
  source?: string | null;
  managed_free?: boolean;
  recommended_model?: string | null;
  free_tier_models?: string[];
};
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
  managed_nine_router_enabled: boolean;
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

export type ModelRouteTestResult = {
  ok: boolean;
  status: number;
  summary: string;
  model: string;
  resolved_model?: string | null;
  response?: string;
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
export type AgentChange = {
  path: string;
  new_content: string;
  diff: string;
  old_sha256?: string;
  new_sha256?: string;
  old_exists?: boolean;
};
export type UploadedImageAsset = {
  ok: boolean;
  path: string;
  name: string;
  content_type?: string | null;
  size: number;
};
