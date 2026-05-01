import React from "react";
import { RefreshCw, ExternalLink, Play } from "lucide-react";

interface PreviewPaneProps {
  ws: string | null;
  previewUrl: string;
  previewFrameKey: number;
  onEnsurePreviewRunning: () => void;
  onHide?: () => void;
  isSmall?: boolean;
}

export const PreviewPane: React.FC<PreviewPaneProps> = ({
  ws,
  previewUrl,
  previewFrameKey,
  onEnsurePreviewRunning,
  onHide,
  isSmall = false,
}) => {
  const browserHost = typeof window !== "undefined" ? window.location.hostname.toLowerCase() : "";
  const hostedBrowser = Boolean(browserHost && !["localhost", "127.0.0.1", "::1"].includes(browserHost));
  const localPreviewTarget = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\]|::1)(:\d+)?/i.test(previewUrl);
  const unreachableHostedPreview = hostedBrowser && localPreviewTarget;
  const hostedPreviewUnavailable = hostedBrowser && !previewUrl;
  const previewState = previewUrl ? "Live preview" : "Preview idle";
  const previewMeta = unreachableHostedPreview
    ? "Hosted mode cannot open localhost preview targets from your browser."
    : hostedPreviewUnavailable
      ? "Hosted demo can edit files, but runtime preview must run from the local desktop app."
      : previewUrl || "Start the app to open a clean embedded viewport.";
  const containerClassName = isSmall ? "previewMiniShell" : "pane previewPane fullAgentPreviewPane";
  const bodyClassName = isSmall ? "previewMiniBody" : "consoleBody sidebarBody previewBody";
  const viewportClassName = isSmall ? "previewEmbedSmall" : "previewViewport";

  return (
    <aside className={containerClassName}>
      <div className={`paneTitle ${isSmall ? "previewMiniHeader" : ""}`}>
        <div>
          <div className="paneEyebrow">Runtime</div>
          <div className="paneHeading">Preview</div>
        </div>
        <div className="editorActions">
          <button className="btn subtleBtn" onClick={onEnsurePreviewRunning} disabled={!ws || hostedPreviewUnavailable}>
            <RefreshCw size={14} />
            <span>{previewUrl ? "Reload" : hostedPreviewUnavailable ? "Hosted only" : "Start"}</span>
          </button>
          {previewUrl && !unreachableHostedPreview ? (
            <a className="btn subtleBtn" href={previewUrl} target="_blank" rel="noreferrer">
              <ExternalLink size={14} />
              <span>Open</span>
            </a>
          ) : null}
          {onHide ? (
            <button className="btn paneToggleBtn" onClick={onHide}>
              Hide
            </button>
          ) : null}
        </div>
      </div>

      <div className={bodyClassName}>
        <div className="previewCanvasWrap">
          {!previewUrl ? (
            <div className="emptyPreview">
              <div className="emptyStateIcon">
                <Play size={18} />
              </div>
              <div className="emptyPreviewTitle">Preview is not running</div>
              <div className="emptyStateText">{hostedPreviewUnavailable ? "Hosted demo can edit files and use the agent, but app runtime preview only works from the local desktop version." : "Launch the selected project and you will get a clean in-app browser surface here."}</div>
              <button className="btn primary" onClick={onEnsurePreviewRunning} disabled={!ws || hostedPreviewUnavailable}>
                <Play size={14} />
                <span>{hostedPreviewUnavailable ? "Preview unavailable here" : "Start preview"}</span>
              </button>
            </div>
          ) : unreachableHostedPreview ? (
            <div className="emptyPreview">
              <div className="emptyStateIcon">
                <ExternalLink size={18} />
              </div>
              <div className="emptyPreviewTitle">Local preview is unavailable in hosted mode</div>
              <div className="emptyStateText">
                This deployed app can edit files and use the agent, but it cannot open a runtime served from localhost inside your browser.
              </div>
            </div>
          ) : (
            <div className={viewportClassName}>
              <div className="previewChrome">
                <div className="previewDots">
                  <span />
                  <span />
                  <span />
                </div>
                <div className="previewAddress">{previewUrl}</div>
              </div>
              <div className="previewFrameWrap">
                <iframe
                  key={`${previewFrameKey}:${previewUrl}`}
                  className="previewFrame"
                  src={previewUrl}
                  style={{ width: "100%", height: "100%", border: 0 }}
                />
              </div>
            </div>
          )}
        </div>

        <div className="previewFooter">
          <div className={`previewStatusPill ${previewUrl ? "live" : "idle"}`}>{previewState}</div>
          <div className="previewFooterMeta" title={previewMeta}>{previewMeta}</div>
        </div>
      </div>
    </aside>
  );
};
