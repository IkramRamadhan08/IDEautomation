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
  nineRouterBaseUrlDraft: string;
  nineRouterApiKeyDraft: string;
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
  onNineRouterBaseUrlChange: (url: string) => void;
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
  nineRouterBaseUrlDraft,
  nineRouterApiKeyDraft,
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
  onNineRouterBaseUrlChange,
  onApiKeyChange,
  onLogout,
  onSave,
}) => {
  if (!settingsOpen) return null;

  const providerKey = "nine_router" as ProviderChoice;
  const providerStatus = providerKey ? settings?.providers?.[providerKey] ?? null : null;
  const modelOptionsId = `model-options-${providerKey || "none"}`;
  const freeModels = providerStatus?.free_tier_models || [];

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
            <input className="settingsInput" value="9Router OpenAI-compatible gateway" readOnly />
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
            <label className="settingsLabel">9Router endpoint</label>
            <input
              value={nineRouterBaseUrlDraft}
              onChange={(e) => onNineRouterBaseUrlChange(e.target.value)}
              className="settingsInput"
              placeholder="http://127.0.0.1:20128/v1 or https://your-9router.app/v1"
            />
            <label className="settingsLabel settingsLabelStacked">9Router API key</label>
            <input type="password" value={nineRouterApiKeyDraft} onChange={(e) => onApiKeyChange("nine_router", e.target.value)} className="settingsInput" placeholder="Paste key from 9Router dashboard" />
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
              placeholder="free-forever, kr/claude-sonnet-4.5, cx/gpt-5.4…"
              disabled={modelsLoading}
            />
            <datalist id={modelOptionsId}>
              {models.map((model) => <option key={model} value={model}>{model}</option>)}
            </datalist>
            <div className="settingsSubtle compactHint">
              {modelsLoading
                ? "Loading…"
                : modelsError
                  || (modelDraft === "free-forever"
                    ? "free-forever memakai combo 9Router. Biaya Appora $0; quota/provider tetap ikut 9Router."
                    : "")
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
