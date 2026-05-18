import React from "react";
import { Bot, Image as ImageIcon, Play, Sparkles } from "lucide-react";
import { PreviewPane } from "../components/preview/PreviewPane";
import { AgentAuditTrail } from "../components/agent/AgentAuditTrail";
import { actionDetail, isOperationalLiveItem } from "../agent/liveActions";
import { getBuildModeProfile } from "../agent/runtime";
import { type AgentAction, type AgentAuditSnapshot, type AgentLiveItem } from "../types";

interface FullAgentWorkspaceProps {
  ws: string | null;
  selectedProject: string;
  previewUrl: string;
  previewFrameKey: number;
  agentStatus: "idle" | "thinking" | "error";
  workingMsg: string;
  agentLog: string;
  agentActions: AgentAction[];
  agentLiveItems: AgentLiveItem[];
  agentAuditTrail: AgentAuditSnapshot[];
  attachedAssetName?: string | null;
  onEnsurePreviewRunning: () => void | Promise<string | void>;
}

function getStatusTone(agentStatus: "idle" | "thinking" | "error", previewUrl: string) {
  if (agentStatus === "error") return "error";
  if (agentStatus === "thinking") return "working";
  if (previewUrl) return "review";
  return "working";
}

export const FullAgentWorkspace: React.FC<FullAgentWorkspaceProps> = ({
  ws,
  selectedProject,
  previewUrl,
  previewFrameKey,
  agentStatus,
  workingMsg,
  agentLog,
  agentActions,
  agentLiveItems,
  agentAuditTrail,
  attachedAssetName,
  onEnsurePreviewRunning,
}) => {
  const profile = getBuildModeProfile("full-agent");
  const statusTone = getStatusTone(agentStatus, previewUrl);
  const logLines = agentLog.split("\n").filter(Boolean).slice(-6);
  const recentActions = agentActions.slice(-4);
  const toolItems = agentLiveItems.filter(isOperationalLiveItem).slice(-6);
  const statusText = agentStatus === "thinking"
    ? workingMsg || "Clara lagi ngerakit preview-nya."
    : agentStatus === "error"
      ? "Clara lagi bongkar problem yang muncul."
      : previewUrl
        ? "Preview sudah hidup, tinggal dorong sampai hasilnya matang."
        : "Kasih brief ke Clara, lalu jalankan preview begitu hasil awal siap.";

  return (
    <div className="fullAgentLayout fullAgentWorkspaceShell">
      <aside className="fullAgentRail fullAgentRailLeft pane">
        <div className="paneTitle">
          <div>
            <div className="paneEyebrow">Full Preview</div>
            <div className="paneHeading">{profile.personaName}</div>
          </div>
          <div className={`previewStatusPill ${previewUrl ? "live" : "idle"}`}>{profile.personaRole}</div>
        </div>
        <div className="sidebarBody fullAgentMissionBody">
          <div className={`missionCard missionStatusCard ${statusTone}`}>
            <div className="missionCardHeader">
              <div>
                <div className="missionCardEyebrow">Mission status</div>
                <div className="missionCardTitle">Clara pegang build, preview jadi pusatnya</div>
              </div>
              <Bot size={16} />
            </div>
            <div className="missionPrimaryText">{statusText}</div>
          </div>

          <div className="missionCard">
            <div className="missionCardHeader">
              <div>
                <div className="missionCardEyebrow">Project</div>
                <div className="missionCardTitle">{selectedProject}</div>
              </div>
              <Sparkles size={16} />
            </div>
            <div className="missionCompactList">
              <div className="missionCompactItem static">
                <div>
                  <div className="missionCompactPrimary">Mode promise</div>
                  <div className="missionCompactMeta">{profile.modeSummary}</div>
                </div>
              </div>
              <div className="missionCompactItem static">
                <div>
                  <div className="missionCompactPrimary">Preview target</div>
                  <div className="missionCompactMeta">{previewUrl ? "A living product surface is already available." : "Clara will push toward a runnable full preview, not just code edits."}</div>
                </div>
              </div>
              {attachedAssetName ? (
                <div className="missionCompactItem static">
                  <div>
                    <div className="missionCompactPrimary">Attached asset</div>
                    <div className="missionCompactMeta">{attachedAssetName}</div>
                  </div>
                  <ImageIcon size={14} />
                </div>
              ) : null}
            </div>
          </div>

        </div>
      </aside>

      <section className="fullAgentCenterStage">
        <div className="missionCard fullAgentStageIntro">
          <div className="missionCardHeader">
            <div>
              <div className="missionCardEyebrow">Full Preview</div>
              <div className="missionCardTitle">Preview-first product stage</div>
            </div>
            <button className="btn primary" onClick={onEnsurePreviewRunning} disabled={!ws} title="Start preview">
              <Play size={14} />
              <span>{previewUrl ? "Refresh preview" : "Start preview"}</span>
            </button>
          </div>
          <div className="missionPrimaryText">This lane keeps the browser preview as the main surface while Clara turns a rough idea into a runnable, polished direction.</div>
        </div>
        <div className="fullAgentPreview">
          <PreviewPane ws={ws} previewUrl={previewUrl} previewFrameKey={previewFrameKey} onEnsurePreviewRunning={onEnsurePreviewRunning} />
        </div>
      </section>

      <aside className="fullAgentRail fullAgentRailRight pane">
        <div className="paneTitle">
          <div>
            <div className="paneEyebrow">Preview delivery</div>
            <div className="paneHeading">What Clara just did</div>
          </div>
          <div className={`previewStatusPill ${agentStatus === "error" ? "idle" : "live"}`}>{agentStatus === "thinking" ? "Building" : agentStatus === "error" ? "Needs review" : "Latest result"}</div>
        </div>
        <div className="sidebarBody fullAgentMissionBody">
          <div className="missionCard">
            <div className="missionCardHeader">
              <div>
                <div className="missionCardEyebrow">Live interaction</div>
                <div className="missionCardTitle">Aksi agent</div>
              </div>
              <Bot size={16} />
            </div>
            <div className="missionCompactList">
              {recentActions.length > 0 ? recentActions.map((action, index) => (
                <div key={`${String(action.type)}-${index}`} className="missionCompactItem static">
                  <div>
                    <div className="missionCompactPrimary">{String(action.type)}</div>
                    <div className="missionCompactMeta">{actionDetail(action)}</div>
                  </div>
                </div>
              )) : null}
              {toolItems.length > 0 ? toolItems.map((item) => (
                <div key={item.id} className="missionCompactItem static">
                  <div>
                    <div className="missionCompactPrimary">tool</div>
                    <div className="missionCompactMeta">{item.text}{item.meta ? ` • ${item.meta}` : ""}</div>
                  </div>
                </div>
              )) : null}
              {recentActions.length === 0 && toolItems.length === 0 ? (
                <div className="missionEmpty">No actions yet. File writes, validation, tools, and audit steps show up here.</div>
              ) : null}
            </div>
          </div>

          <div className="missionCard">
            <div className="missionCardHeader">
              <div>
                <div className="missionCardEyebrow">Audit trail</div>
                <div className="missionCardTitle">Reasoning boundary</div>
              </div>
              <Bot size={16} />
            </div>
            <AgentAuditTrail snapshots={agentAuditTrail} compact />
          </div>

          <div className="missionCard">
            <div className="missionCardHeader">
              <div>
                <div className="missionCardEyebrow">Recent log</div>
                <div className="missionCardTitle">Execution signals</div>
              </div>
              <Bot size={16} />
            </div>
            {logLines.length > 0 ? (
              <div className="missionCompactList">
                {logLines.map((line, index) => (
                  <div key={`${line}-${index}`} className="missionCompactItem static">
                    <div className="missionCompactMeta">{line}</div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="missionEmpty">No logs yet. Validation, preview, and model traces will surface here.</div>
            )}
          </div>
        </div>
      </aside>
    </div>
  );
};
