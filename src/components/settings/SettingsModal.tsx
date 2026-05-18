import React from "react";
import {
  type IdentityInfo,
  type SettingsInfo,
  type ProviderChoice,
  type BuildMode,
  type ModelRouteDiagnostics,
  type ModelRouteTestResult,
} from "../../types";

interface SettingsModalProps {
  settingsOpen: boolean;
  identity: IdentityInfo | null;
  settings: SettingsInfo | null;
  buildModeDraft: BuildMode;
  modelDraft: string;
  nineRouterBaseUrlDraft: string;
  nineRouterApiKeyDraft: string;
  models: string[];
  modelsLoading: boolean;
  modelsError: string;
  modelRouteDiagnostics: ModelRouteDiagnostics | null;
  modelRouteLoading: boolean;
  modelRouteTest: ModelRouteTestResult | null;
  modelRouteTesting: boolean;
  onClose: () => void;
  onBuildModeDraftChange: (mode: BuildMode) => void;
  onModelDraftChange: (model: string) => void;
  onNineRouterBaseUrlChange: (url: string) => void;
  onApiKeyChange: (provider: ProviderChoice, key: string) => void;
  onTestRoute: () => void;
  onLogout: () => void;
  onSave: () => void;
}

export const SettingsModal: React.FC<SettingsModalProps> = ({
  settingsOpen,
  identity,
  settings,
  buildModeDraft,
  modelDraft,
  nineRouterBaseUrlDraft,
  nineRouterApiKeyDraft,
  models,
  modelsLoading,
  modelsError,
  modelRouteDiagnostics,
  modelRouteLoading,
  modelRouteTest,
  modelRouteTesting,
  onClose,
  onBuildModeDraftChange,
  onModelDraftChange,
  onNineRouterBaseUrlChange,
  onApiKeyChange,
  onTestRoute,
  onLogout,
  onSave,
}) => {
  if (!settingsOpen) return null;

  const providerKey = "nine_router" as ProviderChoice;
  const providerStatus = providerKey ? settings?.providers?.[providerKey] ?? null : null;
  const modelOptionsId = `model-options-${providerKey || "none"}`;
  const freeModels = providerStatus?.free_tier_models || [];
  const normalizedEndpoint = nineRouterBaseUrlDraft.trim().replace(/\/$/, "");
  const isLocalNineRouter = /^https?:\/\/(localhost|127\.0\.0\.1)(:20128)?\/v1$/i.test(normalizedEndpoint);
  const managedFreeEnabled = Boolean(settings?.managed_nine_router_enabled || providerStatus?.managed_free || providerStatus?.source === "appora_managed_free");

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
                Clara Preview
              </button>
            </div>
          </div>

          <div className="settingsSection compactSettingsSection">
            <label className="settingsLabel">Provider</label>
            <input className="settingsInput" value="9Router OpenAI-compatible gateway" readOnly />
            {providerStatus ? (
              <div className="providerStatusLine compactStatusLine">
                <span className={`providerStatusChip ${providerStatus.connected ? "connected" : "disconnected"}`}>
                  {managedFreeEnabled ? "Appora Free" : providerStatus.connected ? "Ready" : "Need key"}
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
            <div className="providerStatusLine compactStatusLine">
              <button className="btn subtleBtn miniBtn" type="button" onClick={() => onNineRouterBaseUrlChange("http://127.0.0.1:20128/v1")}>
                Use local 9Router
              </button>
              <a className="btn subtleBtn miniBtn" href="http://127.0.0.1:20128/dashboard" target="_blank" rel="noreferrer">
                Open dashboard
              </a>
            </div>
            <label className="settingsLabel settingsLabelStacked">9Router API key</label>
            <input type="password" value={nineRouterApiKeyDraft} onChange={(e) => onApiKeyChange("nine_router", e.target.value)} className="settingsInput" placeholder={managedFreeEnabled ? "Optional for premium/custom 9Router routes" : "Paste key from 9Router dashboard"} />
            {managedFreeEnabled ? (
              <div className="settingsSubtle compactHint successText">
                Appora Free Router aktif. Model <code>free-forever</code> bisa langsung dipakai tanpa API key user.
              </div>
            ) : (
              <div className="settingsSubtle compactHint">
                Install/run 9Router: <code>npm install -g 9router</code> lalu <code>9router</code>. Endpoint default: <code>http://127.0.0.1:20128/v1</code>.
              </div>
            )}
            {isLocalNineRouter ? (
              <div className="settingsSubtle compactHint">
                Local endpoint hanya works kalau backend Appora jalan di mesin yang sama. Kalau Appora backend hosted/Railway, pakai 9Router tunnel atau URL 9Router yang reachable dari backend.
              </div>
            ) : null}
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
                    ? managedFreeEnabled ? "free-forever memakai Appora Free Router. User bisa langsung ngoding sampai kuota harian habis." : "free-forever memakai combo 9Router. Biaya Appora $0; quota/provider tetap ikut 9Router."
                    : "")
                  || (freeModels.length > 0
                    ? `Free-friendly: ${freeModels.slice(0, 2).join(", ")}. Bisa pilih dari list atau ketik manual.`
                    : providerStatus?.recommended_model
                      ? `Recommended: ${providerStatus.recommended_model}. Bisa pilih dari list atau ketik manual.`
                      : models.length > 0 ? `${models.length} model siap dipilih, atau ketik manual.` : "Belum ada model yang dimuat.")}
            </div>
            <div className="settingsSubtle compactHint">
              {modelRouteLoading
                ? "Checking route availability..."
                : modelRouteDiagnostics
                  ? modelRouteDiagnostics.summary
                  : "Route availability akan dicek setelah model dipilih."}
            </div>
            {modelRouteDiagnostics?.attempts?.length ? (
              <div className="providerStatusLine compactStatusLine">
                {modelRouteDiagnostics.attempts.slice(0, 4).map((attempt) => (
                  <span key={`${attempt.provider}/${attempt.model}`} className={`providerStatusChip ${attempt.connected ? "connected" : "disconnected"}`}>
                    {attempt.provider}/{attempt.model}
                  </span>
                ))}
              </div>
            ) : null}
            {modelRouteDiagnostics && !modelRouteDiagnostics.ok && modelRouteDiagnostics.skipped.length > 0 ? (
              <div className="settingsSubtle compactHint">{modelRouteDiagnostics.skipped.slice(0, 2).join(" · ")}</div>
            ) : null}
            {modelRouteTest ? (
              <div className={`settingsSubtle compactHint ${modelRouteTest.ok ? "successText" : "dangerText"}`}>
                {modelRouteTest.summary}
                {modelRouteTest.resolved_model ? ` Routed via ${modelRouteTest.resolved_model}.` : ""}
              </div>
            ) : null}
          </div>

          <div className="settingsActions settingsSectionWide">
            <button className="btn subtleBtn" onClick={onLogout}>Logout</button>
            <div className="spacer" />
            <button className="btn subtleBtn" onClick={onTestRoute} disabled={modelRouteTesting}>
              {modelRouteTesting ? "Testing..." : "Test route"}
            </button>
            <button className="btn primary" onClick={onSave}>Save changes</button>
          </div>
        </div>
      </div>
    </div>
  );
};
