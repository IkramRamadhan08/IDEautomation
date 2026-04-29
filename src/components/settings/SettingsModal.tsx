import React from "react";
import {
  type IdentityInfo,
  type SettingsInfo,
  type ProviderChoice,
  type BuildMode,
} from "../../types";
import { getBuildModeProfile } from "../../agent/modeProfiles";

interface SettingsModalProps {
  settingsOpen: boolean;
  identity: IdentityInfo | null;
  settings: SettingsInfo | null;
  llmProviderDraft: ProviderChoice;
  buildModeDraft: BuildMode;
  modelDraft: string;
  openaiApiKeyDraft: string;
  anthropicApiKeyDraft: string;
  openrouterApiKeyDraft: string;
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
  llmProviderDraft,
  buildModeDraft,
  modelDraft,
  openaiApiKeyDraft,
  anthropicApiKeyDraft,
  openrouterApiKeyDraft,
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

  const providerKey = llmProviderDraft === "openai-codex" ? "openai_codex" : llmProviderDraft;
  const providerStatus = providerKey ? settings?.providers?.[providerKey] ?? null : null;
  const providerLabel = llmProviderDraft === "openai-codex" ? "OpenAI / Codex" : llmProviderDraft === "anthropic" ? "Anthropic" : llmProviderDraft === "openrouter" ? "OpenRouter" : "provider";
  const hybridProfile = getBuildModeProfile("hybrid");
  const fullAgentProfile = getBuildModeProfile("full-agent");

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

        <div className="settingsGrid">
          <div className="settingsSection">
            <div className="brainTitle">Identity</div>
            <div className="settingsNote">Signed-in profile and workspace mode.</div>
            <div className="settingsInfoCard">
              <div className="settingsProfileLine">
                <span className="mono">{identity?.display_name || identity?.email || identity?.user_id || "loading"}</span>
              </div>
              <div className="settingsSubtle">{identity?.email || "No email available"}</div>
              <div className="providerStatusLine">
                <span className="providerStatusChip">{identity?.managed_workspace_mode === "user" ? "User workspace" : "Session workspace"}</span>
                <span className="settingsSubtle mono">{identity?.managed_workspace_path || "Managed path loading…"}</span>
              </div>
              <div className="providerStatusLine" style={{ marginTop: 8 }}>
                <span className={`providerStatusChip ${settings?.supabase_enabled ? "connected" : "disconnected"}`}>
                  {settings?.supabase_enabled ? "Hosted sync ready" : "Hosted sync not configured"}
                </span>
                <span className="settingsSubtle">{settings?.supabase_enabled ? "Supabase is connected for trial persistence." : "Set Supabase env vars before Vercel trial deployment."}</span>
              </div>
            </div>
          </div>

          <div className="settingsSection">
            <div className="brainTitle">Mode</div>
            <div className="settingsNote">Pick which companion should lead the workflow by default.</div>
            <div className="segmentedControl">
              <button
                className={`btn modeBtn ${buildModeDraft === "hybrid" ? "primary" : ""}`}
                onClick={() => onBuildModeDraftChange("hybrid")}
              >
                Raka / Hybrid
              </button>
              <button
                className={`btn modeBtn ${buildModeDraft === "full-agent" ? "primary" : ""}`}
                onClick={() => onBuildModeDraftChange("full-agent")}
              >
                Clara / Full agent
              </button>
            </div>
            <div className="settingsInfoCard">
              <div className="settingsProfileLine">
                <span>{hybridProfile.personaName}</span>
                <span className="settingsSubtle">{hybridProfile.personaRole}</span>
              </div>
              <div className="settingsSubtle">{hybridProfile.settingsDescription}</div>
              <div className="settingsSubtle">{hybridProfile.modeSummary}</div>
            </div>
            <div className="settingsInfoCard">
              <div className="settingsProfileLine">
                <span>{fullAgentProfile.personaName}</span>
                <span className="settingsSubtle">{fullAgentProfile.personaRole}</span>
              </div>
              <div className="settingsSubtle">{fullAgentProfile.settingsDescription}</div>
              <div className="settingsSubtle">{fullAgentProfile.modeSummary}</div>
            </div>
          </div>

          <div className="settingsSection">
            <label className="settingsLabel">Provider</label>
            <select
              className="settingsInput settingsSelect"
              value={llmProviderDraft}
              onChange={(e) => onLlmProviderChange(e.target.value as ProviderChoice)}
            >
              <option value="">Choose provider…</option>
              <option value="openai-codex">OpenAI / Codex</option>
              <option value="anthropic">Anthropic</option>
              <option value="openrouter">OpenRouter</option>
            </select>
            {providerStatus ? (
              <div className="providerStatusLine">
                <span className={`providerStatusChip ${providerStatus.connected ? "connected" : "disconnected"}`}>
                  {providerStatus.connected ? "BYOK ready" : "API key required"}
                </span>
                <span className="settingsSubtle">{providerStatus.hint || `${providerLabel} is configured through API key only.`}</span>
              </div>
            ) : (
              <div className="settingsSubtle">Choose a provider to configure model and API key.</div>
            )}
          </div>

          <div className="settingsSection">
            <label className="settingsLabel">API key</label>
            {llmProviderDraft === "openai-codex" && (
              <input type="password" value={openaiApiKeyDraft} onChange={(e) => onApiKeyChange("openai-codex", e.target.value)} className="settingsInput" placeholder="sk-..." />
            )}
            {llmProviderDraft === "anthropic" && (
              <input type="password" value={anthropicApiKeyDraft} onChange={(e) => onApiKeyChange("anthropic", e.target.value)} className="settingsInput" placeholder="sk-ant-..." />
            )}
            {llmProviderDraft === "openrouter" && (
              <input type="password" value={openrouterApiKeyDraft} onChange={(e) => onApiKeyChange("openrouter", e.target.value)} className="settingsInput" placeholder="sk-or-..." />
            )}
            {!llmProviderDraft ? <div className="settingsSubtle">Pick a provider first.</div> : null}
          </div>

          <div className="settingsSection">
            <label className="settingsLabel">Model</label>
            <select className="settingsInput settingsSelect" value={modelDraft} onChange={(e) => onModelDraftChange(e.target.value)} disabled={modelsLoading || models.length === 0}>
              {models.length === 0 ? <option value="">No models loaded</option> : null}
              {models.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
            <div className="settingsSubtle">{modelsLoading ? "Loading available models…" : modelsError || "Choose the default model for this workspace. Model providers use BYOK only."}</div>
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
