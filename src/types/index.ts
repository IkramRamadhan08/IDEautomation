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
export type ProviderChoice = "openai" | "anthropic" | "openrouter" | "";
export type BuildMode = "full-agent" | "hybrid";
export type ProviderStatus = {
  provider: "openai" | "anthropic" | "openrouter";
  connected: boolean;
  hint?: string | null;
  profile_id?: string | null;
  account_id?: string | null;
  auth_type?: string | null;
  project_id?: string | null;
  source?: string | null;
};
export type SettingsInfo = {
  default_workspace: string | null;
  llm_provider: "openai" | "anthropic" | "openrouter" | null;
  build_mode: BuildMode;
  openai_model: string;
  anthropic_model: string;
  openrouter_model: string;
  friendly_free_tier_mode: boolean;
  agent_refinement_mode: "auto" | "off" | "always";
  agent_min_gap_seconds: number;
  openai_api_key_set: boolean;
  anthropic_api_key_set: boolean;
  openrouter_api_key_set: boolean;
  supabase_url: string | null;
  supabase_anon_key_set: boolean;
  supabase_enabled: boolean;
  providers: {
    openai: ProviderStatus;
    anthropic: ProviderStatus;
    openrouter: ProviderStatus;
  };
};
export type SettingsUpdate = Partial<{
  default_workspace: string | null;
  llm_provider: ProviderChoice;
  build_mode: BuildMode;
  openai_model: string;
  anthropic_model: string;
  openrouter_model: string;
  friendly_free_tier_mode: boolean;
  agent_refinement_mode: "auto" | "off" | "always";
  agent_min_gap_seconds: number;
  openai_api_key: string | null;
  anthropic_api_key: string | null;
  openrouter_api_key: string | null;
}>;
export type UserPreferences = {
  profile_id: string;
  llm_provider: ProviderChoice | null;
  build_mode: BuildMode | null;
  openai_model: string | null;
  anthropic_model: string | null;
  openrouter_model: string | null;
};
export type ProjectPreferences = {
  project_id: string;
  build_mode: BuildMode | null;
  preview_entry: string | null;
  default_prompt_style: string | null;
};
export type AgentChange = { path: string; new_content: string; diff: string };
export type UploadedImageAsset = {
  ok: boolean;
  path: string;
  name: string;
  content_type?: string | null;
  size: number;
};
