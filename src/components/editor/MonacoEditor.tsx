import React from "react";
import { type OnMount } from "@monaco-editor/react";
import { type AgentAction, type AgentLiveItem, type FileBuffer } from "../../types";
import { PanelBottomClose, PanelBottomOpen, RotateCcw, Save, Sparkles, TerminalSquare, Trash2 } from "lucide-react";
import { terminalRun } from "../../api";

interface MonacoEditorProps {
  activeFile: string;
  openFiles: string[];
  buffers: Record<string, FileBuffer>;
  editorBusy: boolean;
  agentStatus: string;
  editorStatus: string;
  recentActions: AgentAction[];
  agentLiveItems: AgentLiveItem[];
  selectedProject: string;
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
  recentActions,
  agentLiveItems,
  selectedProject,
  onSetActiveFile,
  onCloseFile,
  onRunInlineHelp,
  onSaveFile,
  onOpenFile,
  onBufferChange,
}) => {
  const [activePanel, setActivePanel] = React.useState<"terminal" | "output" | "problems">("terminal");
  const [terminalCommand, setTerminalCommand] = React.useState("");
  const [terminalBusy, setTerminalBusy] = React.useState(false);
  const [terminalVisible, setTerminalVisible] = React.useState(true);
  const [terminalHistory, setTerminalHistory] = React.useState<Array<{
    id: string;
    command: string;
    stdout: string;
    stderr: string;
    returncode: number;
    syncedFiles?: number;
  }>>([]);

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
  const visibleActions = recentActions.slice(-5);
  const visibleTools = agentLiveItems.filter((item) => item.role === "tool").slice(-4);
  const hasActivity = terminalHistory.length > 0 || visibleActions.length > 0 || visibleTools.length > 0;

  const handleEditorMount: OnMount = (editor) => {
    requestAnimationFrame(() => editor.layout());
    window.setTimeout(() => editor.layout(), 80);
  };

  const runFreeShellCommand = async (event: React.FormEvent) => {
    event.preventDefault();
    const command = terminalCommand.trim();
    if (!command || terminalBusy) return;
    setTerminalCommand("");
    setActivePanel("terminal");
    if (command.toLowerCase() === "clear" || command.toLowerCase() === "cls") {
      setTerminalHistory([]);
      return;
    }
    setTerminalBusy(true);
    try {
      const result = await terminalRun(command, selectedProject !== "." ? selectedProject : undefined);
      setTerminalHistory((prev) => [
        ...prev.slice(-8),
        {
          id: `${Date.now()}-${prev.length}`,
          command,
          stdout: result.stdout || "",
          stderr: result.stderr || "",
          returncode: result.returncode,
          syncedFiles: result.synced_files,
        },
      ]);
    } catch (error) {
      setTerminalHistory((prev) => [
        ...prev.slice(-8),
        {
          id: `${Date.now()}-${prev.length}`,
          command,
          stdout: "",
          stderr: error instanceof Error ? error.message : String(error),
          returncode: 1,
          syncedFiles: 0,
        },
      ]);
    } finally {
      setTerminalBusy(false);
    }
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

        {terminalVisible ? (
        <div className="hybridTerminalPanel">
          <div className="terminalTabs">
            {(["problems", "output", "terminal"] as const).map((panel) => (
              <button
                key={panel}
                className={`terminalTab ${activePanel === panel ? "active" : ""}`}
                onClick={() => setActivePanel(panel)}
                type="button"
              >
                {panel}
              </button>
            ))}
            <div className="terminalTabsSpacer" />
            <span className={`terminalStatusDot ${agentStatus === "thinking" ? "running" : agentStatus === "error" ? "error" : ""}`} />
            <span className="terminalStatusText">{agentStatus === "thinking" ? "running" : agentStatus === "error" ? "needs review" : "idle"}</span>
            <button className="terminalIconButton" type="button" onClick={() => setTerminalHistory([])} title="Clear terminal">
              <Trash2 size={14} />
            </button>
            <button className="terminalIconButton" type="button" onClick={() => setTerminalVisible(false)} title="Hide terminal">
              <PanelBottomClose size={15} />
            </button>
          </div>

          <div className="terminalViewport">
            {activePanel === "problems" ? (
              <div className="terminalEmptyState">
                <span>No problems detected in this session.</span>
              </div>
            ) : null}

            {activePanel === "output" ? (
              <div className="terminalLines">
                <div className="terminalLine muted">[appora] {editorStatus || "Ready"}</div>
                <div className="terminalLine muted">[editor] {activeFile ? `Active file: ${activeFile}` : "No active file"}</div>
                <div className="terminalLine muted">[workspace] {openFiles.length} open tab{openFiles.length === 1 ? "" : "s"}</div>
              </div>
            ) : null}

            {activePanel === "terminal" ? (
              <div className="terminalLines">
                <div className="terminalLine prompt">
                  <TerminalSquare size={14} />
                  <span>free shell</span>
                  <span className="terminalLineMeta">user + agent commands</span>
                </div>
                {hasActivity ? null : (
                  <div className="terminalLine muted">Waiting for shell commands, agent commands, and tool activity...</div>
                )}
                {terminalHistory.map((entry) => (
                  <React.Fragment key={entry.id}>
                    <div className="terminalLine">
                      <span className="terminalPrefix">$</span>
                      <span>{entry.command}</span>
                      <span className={`terminalLineMeta ${entry.returncode === 0 ? "success" : "error"}`}>
                        exit {entry.returncode}{typeof entry.syncedFiles === "number" && entry.syncedFiles > 0 ? ` · synced ${entry.syncedFiles}` : ""}
                      </span>
                    </div>
                    {entry.stdout ? (
                      <pre className="terminalPre">{entry.stdout}</pre>
                    ) : null}
                    {entry.stderr ? (
                      <pre className="terminalPre error">{entry.stderr}</pre>
                    ) : null}
                  </React.Fragment>
                ))}
                {visibleActions.map((action, index) => (
                  <div key={`${String(action.type)}-${index}`} className="terminalLine">
                    <span className="terminalPrefix">$</span>
                    <span>{String(action.command || action.type)}</span>
                    {action.path ? <span className="terminalLineMeta">{String(action.path)}</span> : null}
                  </div>
                ))}
                {visibleTools.map((item) => (
                  <div key={item.id} className="terminalLine muted">
                    <span className="terminalPrefix">&gt;</span>
                    <span>{item.text}</span>
                    {item.meta ? <span className="terminalLineMeta">{item.meta}</span> : null}
                  </div>
                ))}
                <form className="terminalInputLine" onSubmit={runFreeShellCommand}>
                  <span className="terminalPrompt">shell $</span>
                  <input
                    className="terminalCommandInput"
                    value={terminalCommand}
                    onChange={(event) => setTerminalCommand(event.target.value)}
                    disabled={terminalBusy}
                    spellCheck={false}
                    autoComplete="off"
                    aria-label="Run shell command"
                  />
                  {terminalBusy ? <span className="terminalLineMeta">running...</span> : <span className="terminalCursor" />}
                </form>
              </div>
            ) : null}
          </div>
        </div>
        ) : (
          <button className="terminalCollapsedBar" type="button" onClick={() => setTerminalVisible(true)}>
            <PanelBottomOpen size={15} />
            <span>Terminal hidden</span>
            <span className="terminalCollapsedHint">Show terminal</span>
          </button>
        )}

        <div className="statusbar">
          <span>{editorBusy ? "Working…" : editorStatus || (activeBuffer?.dirty ? "Unsaved changes" : "Ready")}</span>
          {activeFile ? <span className="mono">{activeFile}</span> : null}
        </div>
      </div>
    </section>
  );
};
