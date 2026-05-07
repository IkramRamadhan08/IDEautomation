import React from "react";
import {
  type IdentityInfo,
  type SettingsInfo,
  type ProviderChoice,
  type BuildMode,
} from "../../types";
import type { AgentCapabilities } from "../../api";

interface SettingsModalProps {
  settingsOpen: boolean;
  identity: IdentityInfo | null;
  settings: SettingsInfo | null;
  agentCapabilities: AgentCapabilities | null;
  llmProviderDraft: ProviderChoice;
  buildModeDraft: BuildMode;
  modelDraft: string;
  openaiApiKeyDraft: string;
  anthropicApiKeyDraft: string;
  openrouterApiKeyDraft: string;
  groqApiKeyDraft: string;
  geminiApiKeyDraft: string;
  togetherApiKeyDraft: string;
  cerebrasApiKeyDraft: string;
  xaiApiKeyDraft: string;
  models: string[];
  modelsLoading: boolean;
  modelsError: string;
  onClose: () => void;
  onLlmProviderChange: (provider: ProviderChoice) => void;
  onBuildModeDraftChange: (mode: BuildMode) => void;
  onModelDraftChange: (model: string) => void;
  onApiKeyChange: (provider: ProviderChoice, key: string) => void;
  onLogout: () => void;
  onSave: () => void;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  settingsOpen,
  identity,
  settings,
  agentCapabilities,
  llmProviderDraft,
  buildModeDraft,
  modelDraft,
  openaiApiKeyDraft,
  anthropicApiKeyDraft,
  openrouterApiKeyDraft,
  groqApiKeyDraft,
  geminiApiKeyDraft,
  togetherApiKeyDraft,
  cerebrasApiKeyDraft,
  xaiApiKeyDraft,
  models,
  modelsLoading,
  modelsError,
  onClose,
  onLlmProviderChange,
  onBuildModeDraftChange,
  onModelDraftChange,
  onApiKeyChange,
  onLogout,
  onSave,
}) => {
  if (!settingsOpen) return null;

  const providerKey = llmProviderDraft;
  const providerStatus = providerKey ? settings?.providers?.[providerKey] ?? null : null;
  const modelOptionsId = `model-options-${providerKey || "none"}`;
  const freeModels = providerStatus?.free_tier_models || [];
  const readiness = [
    {
      label: "Memory",
      value: agentCapabilities?.supports.supabase_rag_ready
        ? "Supabase RAG ready"
        : agentCapabilities?.supports.vector_memory_retrieval
          ? "Local vector fallback"
          : "Session only",
      ok: Boolean(agentCapabilities?.supports.long_term_memory_rag || agentCapabilities?.supports.short_term_memory_rag),
    },
    {
      label: "Tools",
      value: agentCapabilities
        ? `${agentCapabilities.supports.tool_actions.length} action types`
        : "Checking",
      ok: Boolean(agentCapabilities?.supports.skill_registry && agentCapabilities?.supports.read_only_inspection_boundary),
    },
    {
      label: "MCP",
      value: agentCapabilities
        ? `${agentCapabilities.discovered_mcp_servers.length} server detected`
        : "Checking",
      ok: Boolean(agentCapabilities?.supports.mcp_registry),
    },
    {
      label: "Preview QA",
      value: agentCapabilities?.stack.preview_audit_mode || "Checking",
      ok: Boolean(agentCapabilities?.supports.preview_quality_checks || agentCapabilities?.supports.playwright_preview_audit),
    },
  ];

  return (
    <div className="modalBackdrop" onClick={onClose}>
      <div className="modalCard pane" onClick={(e) => e.stopPropagation()}>
        <div className="paneTitle modalHeader">
          <div>
            <div className="paneEyebrow">Configuration</div>
            <div className="paneHeading">Workspace settings</div>
          </div>
          <button className="btn subtleBtn" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="settingsGrid compactSettingsGrid">
          <div className="settingsSection compactSettingsSection settingsSectionWide">
            <div className="settingsRowHead">
              <div className="brainTitle">Mode</div>
              <div className="providerStatusLine compactStatusLine">
                <span className="providerStatusChip">{identity?.managed_workspace_mode === "user" ? "User" : "Session"}</span>
                <span className={`providerStatusChip ${settings?.supabase_rag_status === "ready" ? "connected" : "disconnected"}`}>
                  {settings?.supabase_rag_status === "ready" ? "RAG ready" : settings?.supabase_enabled ? "RAG setup" : "Sync off"}
                </span>
                <span className={`providerStatusChip ${settings?.supabase_frontend_ready ? "connected" : "disconnected"}`}>
                  {settings?.supabase_frontend_ready ? "Auth ready" : "Auth setup"}
                </span>
              </div>
            </div>
            <div className="segmentedControl compactSegmentedControl">
              <button
                className={`btn modeBtn ${buildModeDraft === "hybrid" ? "primary" : ""}`}
                onClick={() => onBuildModeDraftChange("hybrid")}
              >
                Raka
              </button>
              <button
                className={`btn modeBtn ${buildModeDraft === "full-agent" ? "primary" : ""}`}
                onClick={() => onBuildModeDraftChange("full-agent")}
              >
                Clara
              </button>
            </div>
          </div>

          <div className="settingsSection compactSettingsSection settingsSectionWide">
            <div className="settingsRowHead">
              <div>
                <div className="brainTitle">Agent readiness</div>
                <div className="settingsSubtle compactHint">Biar user awam tahu Clara/Raka lagi punya memory, tools, MCP, dan preview check apa.</div>
              </div>
              <div className="providerStatusLine compactStatusLine">
                <span className={`providerStatusChip ${agentCapabilities?.supports.command_conversation_boundary ? "connected" : "disconnected"}`}>
                  Chat boundary
                </span>
                <span className={`providerStatusChip ${agentCapabilities?.supports.provider_fallback_routing ? "connected" : "disconnected"}`}>
                  Fallback
                </span>
              </div>
            </div>
            <div className="settingsReadinessGrid">
              {readiness.map((item) => (
                <div key={item.label} className="settingsReadinessItem">
                  <span className={`settingsReadinessDot ${item.ok ? "ok" : ""}`} />
                  <div>
                    <div className="settingsReadinessLabel">{item.label}</div>
                    <div className="settingsReadinessValue">{item.value}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="settingsSection compactSettingsSection">
            <label className="settingsLabel">Provider</label>
            <select
              className="settingsInput settingsSelect"
              value={llmProviderDraft}
              onChange={(e) => onLlmProviderChange(e.target.value as ProviderChoice)}
            >
              <option value="">Choose provider…</option>
              <option value="openai">OpenAI - familiar / trial credits</option>
              <option value="openrouter">OpenRouter - free/cheap models</option>
              <option value="groq">Groq - fast free plan</option>
              <option value="gemini">Gemini - Google free quota</option>
              <option value="together">Together AI - many open models</option>
              <option value="cerebras">Cerebras - very fast open models</option>
              <option value="xai">xAI - Grok models</option>
              <option value="anthropic">Anthropic - careful edits if you have credits</option>
            </select>
            {providerStatus ? (
              <div className="providerStatusLine compactStatusLine">
                <span className={`providerStatusChip ${providerStatus.connected ? "connected" : "disconnected"}`}>
                  {providerStatus.connected ? "Ready" : "Need key"}
                </span>
                {freeModels.length > 0 ? <span className="providerStatusChip connected">Free models</span> : null}
              </div>
            ) : null}
            {providerStatus?.hint ? <div className="settingsSubtle compactHint">{providerStatus.hint}</div> : null}
          </div>

          <div className="settingsSection">
            <label className="settingsLabel">API key</label>
            {llmProviderDraft === "openai" && (
              <input type="password" value={openaiApiKeyDraft} onChange={(e) => onApiKeyChange("openai", e.target.value)} className="settingsInput" placeholder="sk-..." />
            )}
            {llmProviderDraft === "anthropic" && (
              <input type="password" value={anthropicApiKeyDraft} onChange={(e) => onApiKeyChange("anthropic", e.target.value)} className="settingsInput" placeholder="sk-ant-..." />
            )}
            {llmProviderDraft === "openrouter" && (
              <input type="password" value={openrouterApiKeyDraft} onChange={(e) => onApiKeyChange("openrouter", e.target.value)} className="settingsInput" placeholder="sk-or-..." />
            )}
            {llmProviderDraft === "groq" && (
              <input type="password" value={groqApiKeyDraft} onChange={(e) => onApiKeyChange("groq", e.target.value)} className="settingsInput" placeholder="gsk_..." />
            )}
            {llmProviderDraft === "gemini" && (
              <input type="password" value={geminiApiKeyDraft} onChange={(e) => onApiKeyChange("gemini", e.target.value)} className="settingsInput" placeholder="AIza..." />
            )}
            {llmProviderDraft === "together" && (
              <input type="password" value={togetherApiKeyDraft} onChange={(e) => onApiKeyChange("together", e.target.value)} className="settingsInput" placeholder="tgp_..." />
            )}
            {llmProviderDraft === "cerebras" && (
              <input type="password" value={cerebrasApiKeyDraft} onChange={(e) => onApiKeyChange("cerebras", e.target.value)} className="settingsInput" placeholder="csk-..." />
            )}
            {llmProviderDraft === "xai" && (
              <input type="password" value={xaiApiKeyDraft} onChange={(e) => onApiKeyChange("xai", e.target.value)} className="settingsInput" placeholder="xai-..." />
            )}
            {!llmProviderDraft ? <div className="settingsSubtle">Pick a provider first.</div> : null}
            {settings?.supabase_enabled ? <div className="settingsSubtle compactHint">Saved per account for this hosted deployment.</div> : null}
            {settings?.supabase_warning ? <div className="settingsSubtle compactHint">{settings.supabase_warning}</div> : null}
            {settings?.supabase_missing_env?.length ? (
              <div className="settingsSubtle compactHint">Missing Supabase env: {settings.supabase_missing_env.join(", ")}</div>
            ) : null}
          </div>

          <div className="settingsSection">
            <label className="settingsLabel">Model</label>
            <input
              className="settingsInput"
              value={modelDraft}
              onChange={(e) => onModelDraftChange(e.target.value)}
              list={modelOptionsId}
              placeholder={llmProviderDraft ? "Pilih dari list atau ketik manual…" : "Pilih provider dulu…"}
              disabled={!llmProviderDraft || modelsLoading}
            />
            <datalist id={modelOptionsId}>
              {models.map((model) => <option key={model} value={model}>{model}</option>)}
            </datalist>
            <div className="settingsSubtle compactHint">
              {modelsLoading
                ? "Loading…"
                : modelsError
                  || (freeModels.length > 0
                    ? `Free-friendly: ${freeModels.slice(0, 2).join(", ")}. Bisa pilih dari list atau ketik manual.`
                    : providerStatus?.recommended_model
                      ? `Recommended: ${providerStatus.recommended_model}. Bisa pilih dari list atau ketik manual.`
                      : models.length > 0 ? `${models.length} model siap dipilih, atau ketik manual.` : "Belum ada model yang dimuat.")}
            </div>
          </div>

          <div className="settingsActions settingsSectionWide">
            <button className="btn subtleBtn" onClick={onLogout}>Logout</button>
            <div className="spacer" />
            <button className="btn primary" onClick={onSave}>Save changes</button>
          </div>
        </div>
      </div>
    </div>
  );
};
