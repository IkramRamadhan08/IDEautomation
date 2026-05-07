import React from "react";
import { FileExplorer } from "../components/explorer/FileExplorer";
import { MonacoEditor } from "../components/editor/MonacoEditor";
import { PreviewPane } from "../components/preview/PreviewPane";
import { actionDetail, isOperationalLiveItem } from "../agent/liveActions";
import { type AgentAction, type AgentLiveItem, type ExplorerItem, type FileBuffer } from "../types";

interface HybridWorkspaceProps {
  ws: string | null;
  selectedProject: string;
  projectOptions: Array<{ root: string; name: string }>;
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
  recentActions: AgentAction[];
  agentLiveItems: AgentLiveItem[];
  onRefreshExplorer: () => void | Promise<void>;
  onSelectProject: (project: string) => void;
  onRestoreCheckpoint: () => void | Promise<void>;
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
  projectOptions,
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
  recentActions,
  agentLiveItems,
  onRefreshExplorer,
  onSelectProject,
  onRestoreCheckpoint,
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
  const toolItems = agentLiveItems.filter(isOperationalLiveItem).slice(-4);
  const hasInteractionItems = recentActions.length > 0 || toolItems.length > 0;

  return (
    <div className="hybridLayout hybridWorkspaceShell">
      {showExplorerPane && (
        <FileExplorer
          selectedProject={selectedProject}
          projectOptions={projectOptions}
          explorerItems={explorerItems}
          treeExpanded={treeExpanded}
          treeChildren={treeChildren}
          treeLoading={treeLoading}
          activeFile={activeFile}
          onRefresh={onRefreshExplorer}
          onSelectProject={onSelectProject}
          onRestoreCheckpoint={onRestoreCheckpoint}
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
        recentActions={recentActions}
        agentLiveItems={agentLiveItems}
        selectedProject={selectedProject}
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
            {hasInteractionItems ? (
              <div className="missionCard hybridInteractionCard">
                <div className="missionCardHeader">
                  <div>
                    <div className="missionCardEyebrow">Live interaction</div>
                    <div className="missionCardTitle">Aksi agent</div>
                  </div>
                </div>
                <div className="missionCompactList">
                  {recentActions.slice(-3).map((action, index) => (
                    <div key={`${String(action.type)}-${index}`} className="missionCompactItem static">
                      <div>
                        <div className="missionCompactPrimary">{String(action.type)}</div>
                        <div className="missionCompactMeta">{actionDetail(action)}</div>
                      </div>
                    </div>
                  ))}
                  {toolItems.map((item) => (
                    <div key={item.id} className="missionCompactItem static">
                      <div>
                        <div className="missionCompactPrimary">tool</div>
                        <div className="missionCompactMeta">{item.text}{item.meta ? ` • ${item.meta}` : ""}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ) : null}
            <PreviewPane ws={ws} previewUrl={previewUrl} previewFrameKey={previewFrameKey} onEnsurePreviewRunning={onEnsurePreviewRunning} isSmall />
          </aside>
        </>
      )}
    </div>
  );
};
