import { runStart } from "../api";

export function isHostedBrowser() {
  if (typeof window === "undefined") return false;
  const host = window.location.hostname.toLowerCase();
  return !["localhost", "127.0.0.1", "::1"].includes(host);
}

type EnsurePreviewRunningArgs = {
  workspacePath: string | null;
  selectedProject: string;
  setEditorStatus: (status: string) => void;
  setPreviewUrl: (url: string) => void;
  refreshPreviewFrame: () => void;
  notifyInfo: (message: string) => void;
  notifyError: (message: string) => void;
  errorMessage: (error: unknown) => string;
};

export async function ensurePreviewRunningFlow({
  workspacePath,
  selectedProject,
  setEditorStatus,
  setPreviewUrl,
  refreshPreviewFrame,
  notifyInfo,
  notifyError,
  errorMessage,
}: EnsurePreviewRunningArgs): Promise<string> {
  if (!workspacePath) return "";

  setEditorStatus(`Starting preview for ${selectedProject}...`);
  try {
    const run = await runStart(selectedProject);
    setPreviewUrl(run.url);
    refreshPreviewFrame();
    setEditorStatus(`Preview live at ${run.url}`);
    return run.url;
  } catch (error) {
    setEditorStatus("Failed to start preview");
    notifyError(`Gagal menjalankan preview: ${errorMessage(error)}`);
    return "";
  }
}
