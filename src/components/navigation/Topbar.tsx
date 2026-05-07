import React from "react";
import { type BuildMode, type IdentityInfo } from "../../types";
import { Settings, Play, Command, PanelLeft, PanelRight, CircleDot, Search } from "lucide-react";

interface TopbarProps {
  ws: string | null;
  identity: IdentityInfo | null;
  previewUrl: string;
  buildMode: BuildMode;
  showExplorerPane: boolean;
  showAssistPane: boolean;
  onQuickSwitchBuildMode: (mode: BuildMode) => void;
  onOpenSettings: () => void;
  onEnsurePreviewRunning: () => void;
  onToggleExplorerPane: () => void;
  onToggleAssistPane: () => void;
}

export const Topbar: React.FC<TopbarProps> = ({
  ws,
  identity,
  previewUrl,
  buildMode,
  showExplorerPane,
  showAssistPane,
  onQuickSwitchBuildMode,
  onOpenSettings,
  onEnsurePreviewRunning,
  onToggleExplorerPane,
  onToggleAssistPane,
}) => {
  const userLabel = identity?.display_name || identity?.email || "Signed in";
  const userInitial = userLabel.trim().charAt(0).toUpperCase() || "V";
  const browserHost = typeof window !== "undefined" ? window.location.hostname.toLowerCase() : "";
  const hostedPreviewUnavailable = Boolean(browserHost && !["localhost", "127.0.0.1", "::1"].includes(browserHost) && !previewUrl);

  return (
    <header className="topbar">
      <div className="brandBlock">
        <div className="brandMark">
          <Command size={15} />
        </div>
        <div className="brandStack">
          <div className="brand">Appora</div>
        </div>
      </div>

      <div className="topbarMeta minimalTopbarMeta">
        <span className={`topbarPill ${previewUrl ? "success" : ""}`}>
          <CircleDot size={12} />
          {previewUrl ? "Live" : "Ready"}
        </span>
      </div>

      <button className="commandBarButton" type="button" onClick={onOpenSettings} title="Command menu">
        <Search size={14} />
        <span>Search settings and models</span>
        <kbd>⌘K</kbd>
      </button>

      <div className="spacer" />

      <div className="topbarModeSwitch">
        <button
          className={`btn modeBtn ${buildMode === "hybrid" ? "primary" : ""}`}
          onClick={() => onQuickSwitchBuildMode("hybrid")}
        >
          Raka
        </button>
        <button
          className={`btn modeBtn ${buildMode === "full-agent" ? "primary" : ""}`}
          onClick={() => onQuickSwitchBuildMode("full-agent")}
        >
          Clara
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

        <button className="btn primary iconBtn" disabled={!ws || hostedPreviewUnavailable} onClick={onEnsurePreviewRunning} title={hostedPreviewUnavailable ? "Preview tidak tersedia di deployment ini" : "Preview"}>
          <Play size={16} />
          <span>{hostedPreviewUnavailable ? "Off" : "Preview"}</span>
        </button>

        <div className="topbarUserBadge" title={userLabel}>
          <span className="topbarUserAvatar">{userInitial}</span>
          <span className="topbarUserText">{userLabel}</span>
        </div>
      </div>
    </header>
  );
};
