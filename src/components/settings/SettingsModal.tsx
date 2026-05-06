import React from "react";
import {
  type IdentityInfo,
  type SettingsInfo,
  type ProviderChoice,
  type BuildMode,
} from "../../types";

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

  const providerKey = llmProviderDraft;
  const providerStatus = providerKey ? settings?.providers?.[providerKey] ?? null : null;
  const modelOptionsId = `model-options-${providerKey || "none"}`;

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

          <div className="settingsSection compactSettingsSection">
            <label className="settingsLabel">Provider</label>
            <select
              className="settingsInput settingsSelect"
              value={llmProviderDraft}
              onChange={(e) => onLlmProviderChange(e.target.value as ProviderChoice)}
            >
              <option value="">Choose provider…</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="openrouter">OpenRouter</option>
            </select>
            {providerStatus ? (
              <div className="providerStatusLine compactStatusLine">
                <span className={`providerStatusChip ${providerStatus.connected ? "connected" : "disconnected"}`}>
                  {providerStatus.connected ? "Ready" : "Need key"}
                </span>
              </div>
            ) : null}
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
              {modelsLoading ? "Loading…" : modelsError || (models.length > 0 ? `${models.length} model siap dipilih, atau ketik manual.` : "Belum ada model yang dimuat.")}
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
