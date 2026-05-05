import React from "react";
import { FileExplorer } from "../components/explorer/FileExplorer";
import { MonacoEditor } from "../components/editor/MonacoEditor";
import { PreviewPane } from "../components/preview/PreviewPane";
import { AgentLiveStage } from "../components/agent/AgentLiveStage";
import { AgentAuditTrail } from "../components/agent/AgentAuditTrail";
import { getBuildModeProfile } from "../agent/runtime";
import { type AgentAction, type AgentAuditSnapshot, type AgentLiveItem, type ExplorerItem, type FileBuffer } from "../types";

interface HybridWorkspaceProps {
  ws: string | null;
  selectedProject: string;
  explorerItems: ExplorerItem[];
  treeExpanded: Record<string, boolean>;
  treeChildren: Record<string, ExplorerItem[]>;
  treeLoading: Record<string, boolean>;
  activeFile: string;
  openFiles: string[];
  buffers: Record<string, FileBuffer>;
  editorBusy: boolean;
  agentStatus: string;
  editorStatus: string;
  showExplorerPane: boolean;
  showAssistPane: boolean;
  assistPaneWidth: number;
  isResizingAssistPane: boolean;
  previewUrl: string;
  previewFrameKey: number;
  attachedAssetName?: string | null;
  recentActions: AgentAction[];
  agentLiveItems: AgentLiveItem[];
  agentAuditTrail: AgentAuditSnapshot[];
  onRefreshExplorer: () => void | Promise<void>;
  onToggleDir: (path: string) => void | Promise<void>;
  onOpenFile: (path: string) => void | Promise<void>;
  onHideExplorer: () => void;
  onNewFile: () => void | Promise<void>;
  onSetActiveFile: (path: string) => void;
  onCloseFile: (path: string) => void;
  onRunInlineHelp: () => void | Promise<void>;
  onSaveFile: () => void | Promise<void>;
  onBufferChange: (path: string, content: string) => void;
  onStartResizeAssistPane: () => void;
  onEnsurePreviewRunning: () => void | Promise<string | void>;
}

export const HybridWorkspace: React.FC<HybridWorkspaceProps> = ({
  ws,
  selectedProject,
  explorerItems,
  treeExpanded,
  treeChildren,
  treeLoading,
  activeFile,
  openFiles,
  buffers,
  editorBusy,
  agentStatus,
  editorStatus,
  showExplorerPane,
  showAssistPane,
  assistPaneWidth,
  isResizingAssistPane,
  previewUrl,
  previewFrameKey,
  attachedAssetName,
  recentActions,
  agentLiveItems,
  agentAuditTrail,
  onRefreshExplorer,
  onToggleDir,
  onOpenFile,
  onHideExplorer,
  onNewFile,
  onSetActiveFile,
  onCloseFile,
  onRunInlineHelp,
  onSaveFile,
  onBufferChange,
  onStartResizeAssistPane,
  onEnsurePreviewRunning,
}) => {
  const profile = getBuildModeProfile("hybrid");
  const activeFileName = activeFile.split("/").pop() || "No file selected";
  const recentAction = recentActions.length > 0 ? recentActions[recentActions.length - 1] : undefined;

  return (
    <div className="hybridLayout hybridWorkspaceShell">
      {showExplorerPane && (
        <FileExplorer
          selectedProject={selectedProject}
          explorerItems={explorerItems}
          treeExpanded={treeExpanded}
          treeChildren={treeChildren}
          treeLoading={treeLoading}
          activeFile={activeFile}
          onRefresh={onRefreshExplorer}
          onToggleDir={onToggleDir}
          onOpenFile={onOpenFile}
          onHide={onHideExplorer}
          onNewFile={onNewFile}
        />
      )}

      <MonacoEditor
        activeFile={activeFile}
        openFiles={openFiles}
        buffers={buffers}
        editorBusy={editorBusy}
        agentStatus={agentStatus}
        editorStatus={editorStatus}
        onSetActiveFile={onSetActiveFile}
        onCloseFile={onCloseFile}
        onRunInlineHelp={onRunInlineHelp}
        onSaveFile={onSaveFile}
        onOpenFile={onOpenFile}
        onBufferChange={onBufferChange}
      />

      {showAssistPane && (
        <>
          <div
            className={`assistResizeHandle ${isResizingAssistPane ? "active" : ""}`}
            onMouseDown={onStartResizeAssistPane}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize assist panel"
          />
          <aside className="hybridSidePane hybridAssistRail" style={{ width: assistPaneWidth }}>
            <div className="hybridAssistSummary missionCard">
              <div className="missionCardHeader">
                <div>
                  <div className="missionCardEyebrow">{profile.personaName}</div>
                  <div className="missionCardTitle">{profile.personaRole}</div>
                </div>
                <div className={`previewStatusPill ${previewUrl ? "live" : "idle"}`}>
                  {previewUrl ? "Watching preview" : "Watching editor"}
                </div>
              </div>
              <div className="missionPrimaryText">{profile.modeSummary}</div>
              <div className="missionCompactList">
                <div className="missionCompactItem static">
                  <div>
                    <div className="missionCompactPrimary">Current focus</div>
                    <div className="missionCompactMeta">{activeFileName}</div>
                  </div>
                </div>
                <div className="missionCompactItem static">
                  <div>
                    <div className="missionCompactPrimary">Open tabs</div>
                    <div className="missionCompactMeta">{openFiles.length} file active in the coding lane</div>
                  </div>
                </div>
                <div className="missionCompactItem static">
                  <div>
                    <div className="missionCompactPrimary">Latest signal</div>
                    <div className="missionCompactMeta">{recentAction ? `${String(recentAction.type)} ${String(recentAction.command || recentAction.path || "")}` : editorStatus}</div>
                  </div>
                </div>
                {attachedAssetName ? (
                  <div className="missionCompactItem static">
                    <div>
                      <div className="missionCompactPrimary">Attached asset</div>
                      <div className="missionCompactMeta">{attachedAssetName}</div>
                    </div>
                  </div>
                ) : null}
              </div>
            </div>
            {agentLiveItems.length > 0 || agentStatus === "thinking" ? (
              <div className="missionCard hybridLiveCard">
                <div className="missionCardHeader">
                  <div>
                    <div className="missionCardEyebrow">Live interaction</div>
                    <div className="missionCardTitle">Raka lagi jelasin langkahnya</div>
                  </div>
                </div>
                <AgentLiveStage
                  items={agentLiveItems.slice(-6)}
                  agentStatus={agentStatus === "thinking" ? "thinking" : agentStatus === "error" ? "error" : "idle"}
                  workingMsg={editorStatus}
                  compact
                />
              </div>
            ) : null}
            {agentAuditTrail.length > 0 ? (
              <div className="missionCard hybridLiveCard">
                <div className="missionCardHeader">
                  <div>
                    <div className="missionCardEyebrow">Audit trail</div>
                    <div className="missionCardTitle">Jejak reasoning yang bisa diaudit</div>
                  </div>
                </div>
                <AgentAuditTrail snapshots={agentAuditTrail} compact />
              </div>
            ) : null}
            <PreviewPane ws={ws} previewUrl={previewUrl} previewFrameKey={previewFrameKey} onEnsurePreviewRunning={onEnsurePreviewRunning} isSmall />
          </aside>
        </>
      )}
    </div>
  );
};
