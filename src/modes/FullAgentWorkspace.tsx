import React from "react";
import { Play, RefreshCw } from "lucide-react";
import { PreviewPane } from "../components/preview/PreviewPane";

interface FullAgentWorkspaceProps {
  ws: string | null;
  selectedProject: string;
  previewUrl: string;
  previewFrameKey: number;
  agentStatus: "idle" | "thinking" | "error";
  workingMsg: string;
  onEnsurePreviewRunning: () => void | Promise<string | void>;
}

export const FullAgentWorkspace: React.FC<FullAgentWorkspaceProps> = ({
  ws,
  selectedProject,
  previewUrl,
  previewFrameKey,
  agentStatus,
  workingMsg,
  onEnsurePreviewRunning,
}) => {
  const statusText = agentStatus === "thinking"
    ? workingMsg || "Appora Agent sedang membangun preview."
    : agentStatus === "error"
      ? "Preview butuh review."
      : previewUrl
        ? "Preview live"
        : "Preview belum jalan";

  return (
    <div className="fullAgentLayout fullAgentWorkspaceShell">
      <div className="fullPreviewHeader">
        <div className="fullPreviewMeta">
          <span className={`previewStatusPill ${previewUrl ? "live" : "idle"}`}>{statusText}</span>
          <span className="fullPreviewProject">{selectedProject}</span>
        </div>
        <button className="btn primary iconBtn" onClick={onEnsurePreviewRunning} disabled={!ws} title="Preview">
          {previewUrl ? <RefreshCw size={15} /> : <Play size={15} />}
          <span>{previewUrl ? "Refresh" : "Start preview"}</span>
        </button>
      </div>

      <div className="fullAgentPreview">
        <PreviewPane ws={ws} previewUrl={previewUrl} previewFrameKey={previewFrameKey} onEnsurePreviewRunning={onEnsurePreviewRunning} />
      </div>
    </div>
  );
};
