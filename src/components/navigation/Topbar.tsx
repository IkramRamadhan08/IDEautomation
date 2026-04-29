import React from "react";
import { type BuildMode, type IdentityInfo, type ProjectInfo } from "../../types";
import { getBuildModeProfile } from "../../agent/modeProfiles";
import { FolderOpen, Settings, Play, Command, PanelLeft, PanelRight, CircleDot } from "lucide-react";

interface TopbarProps {
  ws: string | null;
  identity: IdentityInfo | null;
  previewUrl: string;
  buildMode: BuildMode;
  projects: ProjectInfo[];
  selectedProject: string;
  showExplorerPane: boolean;
  showAssistPane: boolean;
  onQuickSwitchBuildMode: (mode: BuildMode) => void;
  onPickWorkspace: () => void;
  onOpenSettings: () => void;
  onSelectProject: (project: string) => void;
  onEnsurePreviewRunning: () => void;
  onToggleExplorerPane: () => void;
  onToggleAssistPane: () => void;
}

export const Topbar: React.FC<TopbarProps> = ({
  ws,
  identity,
  previewUrl,
  buildMode,
  projects,
  selectedProject,
  showExplorerPane,
  showAssistPane,
  onQuickSwitchBuildMode,
  onPickWorkspace,
  onOpenSettings,
  onSelectProject,
  onEnsurePreviewRunning,
  onToggleExplorerPane,
  onToggleAssistPane,
}) => {
  const selectedProjectInfo = projects.find((project) => project.root === selectedProject) ?? null;
  const workspaceName = ws ? ws.split("/").filter(Boolean).pop() || ws : "No workspace";
  const userLabel = identity?.display_name || identity?.email || "Signed in";
  const userInitial = userLabel.trim().charAt(0).toUpperCase() || "V";
  const modeProfile = getBuildModeProfile(buildMode);

  return (
    <header className="topbar">
      <div className="brandBlock">
        <div className="brandMark">
          <Command size={15} />
        </div>
        <div className="brandStack">
          <div className="brand">Voice IDE</div>
          <div className="brandSub">{modeProfile.topbarSubtitle}</div>
        </div>
      </div>

      <div className="topbarMeta">
        <span className={`topbarPill ${previewUrl ? "success" : ""}`}>
          <CircleDot size={12} />
          {previewUrl ? "Preview live" : "Ready to build"}
        </span>
        <span className="topbarPill">{workspaceName}</span>
        <span className="topbarPath" title={selectedProject}>{selectedProjectInfo?.name || "No project selected"}</span>
        {selectedProjectInfo?.has_dev ? <span className="topbarPill success">Dev script detected</span> : null}
      </div>

      <div className="spacer" />

      <div className="topbarModeSwitch">
        <button
          className={`btn modeBtn ${buildMode === "hybrid" ? "primary" : ""}`}
          onClick={() => onQuickSwitchBuildMode("hybrid")}
        >
          Raka / Hybrid
        </button>
        <button
          className={`btn modeBtn ${buildMode === "full-agent" ? "primary" : ""}`}
          onClick={() => onQuickSwitchBuildMode("full-agent")}
        >
          Clara / Full Agent
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

        <div className="topbarSelectWrap">
          <select
            className="topbarSelect"
            value={selectedProject}
            disabled={!ws || projects.length === 0}
            onChange={(e) => onSelectProject(e.target.value)}
            aria-label="Project"
          >
            {projects.length === 0 ? <option value=".">No project</option> : null}
            {projects.map((project) => (
              <option key={project.root} value={project.root}>
                {project.name}
              </option>
            ))}
          </select>
        </div>

        <button className="btn iconBtn" onClick={onPickWorkspace} title="Open folder">
          <FolderOpen size={16} />
          <span>Folder</span>
        </button>

        <button className="btn iconBtn" onClick={onOpenSettings} title="Settings">
          <Settings size={16} />
          <span>Settings</span>
        </button>

        <button className="btn primary iconBtn" disabled={!ws} onClick={onEnsurePreviewRunning}>
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
