import { PreviewPane } from "../components/preview/PreviewPane";

interface FullAgentWorkspaceProps {
  ws: string | null;
  previewUrl: string;
  previewFrameKey: number;
  onEnsurePreviewRunning: () => void | Promise<string | void>;
}

export const FullAgentWorkspace: React.FC<FullAgentWorkspaceProps> = ({
  ws,
  previewUrl,
  previewFrameKey,
  onEnsurePreviewRunning,
}) => {
  return (
    <div className="fullAgentLayout fullAgentWorkspaceShell">
      <div className="fullAgentPreview">
        <PreviewPane ws={ws} previewUrl={previewUrl} previewFrameKey={previewFrameKey} onEnsurePreviewRunning={onEnsurePreviewRunning} />
      </div>
    </div>
  );
};
