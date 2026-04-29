import React from "react";
import { type OnMount } from "@monaco-editor/react";
import { type FileBuffer } from "../../types";
import { Save, RotateCcw, Sparkles } from "lucide-react";

interface MonacoEditorProps {
  activeFile: string;
  openFiles: string[];
  buffers: Record<string, FileBuffer>;
  editorBusy: boolean;
  agentStatus: string;
  editorStatus: string;
  onSetActiveFile: (path: string) => void;
  onCloseFile: (path: string) => void;
  onRunInlineHelp: () => void;
  onSaveFile: () => void;
  onOpenFile: (path: string) => void;
  onBufferChange: (path: string, content: string) => void;
}

const LazyEditor = React.lazy(() => import("@monaco-editor/react"));

export const MonacoEditor: React.FC<MonacoEditorProps> = ({
  activeFile,
  openFiles,
  buffers,
  editorBusy,
  agentStatus,
  editorStatus,
  onSetActiveFile,
  onCloseFile,
  onRunInlineHelp,
  onSaveFile,
  onOpenFile,
  onBufferChange,
}) => {
  const languageForFile = (path: string) => {
    const lower = path.toLowerCase();
    if (lower.endsWith(".tsx")) return "typescript";
    if (lower.endsWith(".ts")) return "typescript";
    if (lower.endsWith(".jsx")) return "javascript";
    if (lower.endsWith(".js") || lower.endsWith(".mjs") || lower.endsWith(".cjs")) return "javascript";
    if (lower.endsWith(".json")) return "json";
    if (lower.endsWith(".css")) return "css";
    if (lower.endsWith(".scss")) return "scss";
    if (lower.endsWith(".html") || lower.endsWith(".htm")) return "html";
    if (lower.endsWith(".md")) return "markdown";
    if (lower.endsWith(".py")) return "python";
    if (lower.endsWith(".sh")) return "shell";
    return "plaintext";
  };

  const activeBuffer = activeFile ? buffers[activeFile] : undefined;

  const handleEditorMount: OnMount = (editor) => {
    requestAnimationFrame(() => editor.layout());
    window.setTimeout(() => editor.layout(), 80);
  };

  return (
    <section className="pane hybridEditorPane">
      <div className="tabs">
        {openFiles.map((file) => (
          <div
            key={file}
            className={`tab ${activeFile === file ? "active" : ""}`}
            onClick={() => onSetActiveFile(file)}
          >
            <span className="tabLabel">{file.split("/").pop()}</span>
            {buffers[file]?.dirty ? <span className="tag">Edited</span> : null}
            <button
              className="tabClose"
              onClick={(e) => {
                e.stopPropagation();
                onCloseFile(file);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </div>

      <div className="editorWrap">
        <div className="editorToolbar">
          <div className="editorToolbarMeta">
            <div className="editorTitle">{activeFile ? activeFile.split("/").pop() : "No file selected"}</div>
            <div className="editorHint">
              {activeFile ? `${languageForFile(activeFile)} file` : "Choose a file from the explorer to start editing."}
            </div>
          </div>

          <div className="editorActions">
            <button
              className="btn subtleBtn"
              onClick={onRunInlineHelp}
              disabled={!activeFile || editorBusy || agentStatus === "thinking"}
            >
              <Sparkles size={14} />
              <span>Assist</span>
            </button>
            <button
              className="btn subtleBtn"
              onClick={() => onSaveFile()}
              disabled={!activeFile || !activeBuffer?.dirty || editorBusy}
            >
              <Save size={14} />
              <span>Save</span>
            </button>
            <button
              className="btn subtleBtn"
              onClick={() => activeFile && onOpenFile(activeFile)}
              disabled={!activeFile || editorBusy}
            >
              <RotateCcw size={14} />
              <span>Reload</span>
            </button>
          </div>
        </div>

        <div className="hybridWorkbench">
          <div className="hybridPrimarySurface">
            {activeFile ? (
              <div className="codeEditorShell">
                <React.Suspense fallback={<div className="emptyEditor"><div className="emptyState"><div className="emptyStateTitle">Loading editor…</div><div className="emptyStateText">Monaco is loading only when you actually open a file.</div></div></div>}>
                  <LazyEditor
                    key={activeFile}
                    path={activeFile}
                    height="100%"
                    width="100%"
                    language={languageForFile(activeFile)}
                    value={activeBuffer?.content || ""}
                    onMount={handleEditorMount}
                    onChange={(next) => onBufferChange(activeFile, next ?? "")}
                    theme="vs-dark"
                    options={{
                      automaticLayout: true,
                      minimap: { enabled: false },
                      fontSize: 13,
                      lineHeight: 22,
                      tabSize: 2,
                      smoothScrolling: true,
                      scrollBeyondLastLine: false,
                      wordWrap: "on",
                      padding: { top: 18, bottom: 18 },
                    }}
                  />
                </React.Suspense>
              </div>
            ) : (
              <div className="emptyEditor">
                <div className="emptyState">
                  <div className="emptyStateTitle">Open a file to begin</div>
                  <div className="emptyStateText">
                    Keep the layout calm, edit deliberately, and use Assist when you want Raka to help with a focused change.
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="statusbar">
          <span>{editorBusy ? "Working…" : editorStatus || (activeBuffer?.dirty ? "Unsaved changes" : "Ready")}</span>
          {activeFile ? <span className="mono">{activeFile}</span> : null}
        </div>
      </div>
    </section>
  );
};
