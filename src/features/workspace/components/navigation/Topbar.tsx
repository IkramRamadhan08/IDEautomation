import React from "react";
import { type BuildMode, type IdentityInfo } from "../../../../shared/types";
import { Settings, Play, Command, PanelLeft, PanelRight, Search, Moon, Sun, FolderKanban, Download, Route, Activity } from "lucide-react";

interface TopbarProps {
  ws: string | null;
  identity: IdentityInfo | null;
  previewUrl: string;
  buildMode: BuildMode;
  appTheme: "light" | "dark";
  showExplorerPane: boolean;
  showAssistPane: boolean;
  onQuickSwitchBuildMode: (mode: BuildMode) => void;
  onToggleTheme: () => void;
  onOpenSettings: () => void;
  onOpenProjects: () => void;
  onDownloadProject: () => void;
  onEnsurePreviewRunning: () => void;
  onToggleExplorerPane: () => void;
  onToggleAssistPane: () => void;
}

export const Topbar: React.FC<TopbarProps> = ({
  ws,
  identity,
  previewUrl,
  buildMode,
  appTheme,
  showExplorerPane,
  showAssistPane,
  onQuickSwitchBuildMode,
  onToggleTheme,
  onOpenSettings,
  onOpenProjects,
  onDownloadProject,
  onEnsurePreviewRunning,
  onToggleExplorerPane,
  onToggleAssistPane,
}) => {
  const userLabel = identity?.display_name || identity?.email || "Signed in";
  const userInitial = userLabel.trim().charAt(0).toUpperCase() || "V";
  const workspaceLabel = buildMode === "hybrid" ? "IDE workspace" : "Preview desk";

  return (
    <header className="topbar">
      <div className="brandBlock">
        <div className="brandMark">
          <Command size={15} />
        </div>
        <div className="brandStack">
          <div className="brand">Appora Studio</div>
          <div className="brandSub">agentic coder workspace</div>
        </div>
      </div>

      <div className="topbarMeta minimalTopbarMeta">
        <span className={`topbarPill ${previewUrl ? "success" : ""}`}>
          <Activity size={12} />
          {previewUrl ? "Live" : "Ready"}
        </span>
        <span className="topbarPath">
          <Route size={12} />
          {workspaceLabel}
        </span>
      </div>

      <button className="commandBarButton" type="button" onClick={onOpenSettings} title="Command menu">
        <Search size={14} />
        <span>Settings, model routes, projects</span>
        <kbd>⌘K</kbd>
      </button>

      <div className="spacer" />

      <div className="topbarModeSwitch" aria-label="Layout switch" title="Same Appora Agent, different workspace layout">
        <span className="topbarModeLabel">Flow</span>
        <button
          className={`btn modeBtn ${buildMode === "hybrid" ? "primary" : ""}`}
          onClick={() => onQuickSwitchBuildMode("hybrid")}
          title="Editor-first workspace with the same Appora Agent"
        >
          IDE
        </button>
        <button
          className={`btn modeBtn ${buildMode === "full-agent" ? "primary" : ""}`}
          onClick={() => onQuickSwitchBuildMode("full-agent")}
          title="Large preview layout with the same Appora Agent"
        >
          Preview
        </button>
      </div>

      <div className="topbarActions">
        <div className="topbarPanelToggles">
          {buildMode === "hybrid" ? (
            <>
              <button className={`btn panelToggleBtn ${showExplorerPane ? "primary" : ""}`} onClick={onToggleExplorerPane} title="Toggle files panel">
                <PanelLeft size={15} />
                <span>Files</span>
              </button>
              <button className={`btn panelToggleBtn ${showAssistPane ? "primary" : ""}`} onClick={onToggleAssistPane} title="Toggle assist panel">
                <PanelRight size={15} />
                <span>Assist</span>
              </button>
            </>
          ) : null}
        </div>

        <button className="btn iconBtn" onClick={onOpenSettings} title="Settings">
          <Settings size={16} />
          <span>Settings</span>
        </button>

        <button className="btn iconBtn" onClick={onOpenProjects} title="Projects">
          <FolderKanban size={16} />
          <span>Projects</span>
        </button>

        <button className="btn iconBtn" disabled={!ws} onClick={onDownloadProject} title="Download project ZIP">
          <Download size={16} />
          <span>Save</span>
        </button>

        <button className="btn iconBtn themeToggleBtn" onClick={onToggleTheme} title={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`}>
          {appTheme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          <span>{appTheme === "dark" ? "Light" : "Dark"}</span>
        </button>

        <button className="btn primary iconBtn" disabled={!ws} onClick={onEnsurePreviewRunning} title="Preview">
          <Play size={16} />
          <span>Preview</span>
        </button>

        <div className="topbarUserBadge" title={userLabel}>
          <span className="topbarUserAvatar">{userInitial}</span>
          <span className="topbarUserText">{userLabel}</span>
        </div>
      </div>
    </header>
  );
};
