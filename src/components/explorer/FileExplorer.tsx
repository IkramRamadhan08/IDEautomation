import React from "react";
import { type ExplorerItem } from "../../types";
import { Folder, FolderOpen as FolderOpenIcon, File, Plus, RefreshCw, RotateCcw } from "lucide-react";

interface FileExplorerProps {
  selectedProject: string;
  projectOptions: Array<{ root: string; name: string }>;
  explorerItems: ExplorerItem[];
  treeExpanded: Record<string, boolean>;
  treeChildren: Record<string, ExplorerItem[]>;
  treeLoading: Record<string, boolean>;
  activeFile: string;
  onRefresh: () => void | Promise<void>;
  onSelectProject: (project: string) => void;
  onRestoreCheckpoint: () => void | Promise<void>;
  onToggleDir: (path: string) => void;
  onOpenFile: (path: string) => void;
  onHide: () => void;
  onNewFile: () => void;
}

export const FileExplorer: React.FC<FileExplorerProps> = ({
  selectedProject,
  projectOptions,
  explorerItems,
  treeExpanded,
  treeChildren,
  treeLoading,
  activeFile,
  onRefresh,
  onSelectProject,
  onRestoreCheckpoint,
  onToggleDir,
  onOpenFile,
  onHide,
  onNewFile,
}) => {
  const projectLabel = selectedProject === "." ? "Workspace" : selectedProject.split("/").filter(Boolean).pop() || selectedProject;

  const renderTree = (items: ExplorerItem[], depth = 0) => {
    return items.map((item) => {
      const isDir = item.type === "dir";
      const expanded = !!treeExpanded[item.path];
      const loading = !!treeLoading[item.path];
      const children = treeChildren[item.path] || [];
      const isActive = activeFile === item.path;

      return (
        <div key={item.path}>
          <button
            className={`treeRow ${isActive ? "active" : ""} ${isDir ? "dir" : "file"}`}
            style={{ paddingLeft: 12 + depth * 14 }}
            onClick={() => {
              if (isDir) {
                onToggleDir(item.path);
              } else {
                onOpenFile(item.path);
              }
            }}
          >
            <span className={`treeChevron ${isDir && expanded ? "open" : ""}`}>{isDir ? "▸" : "•"}</span>
            <span className={`treeIcon ${isDir ? "dir" : "file"}`}>
              {isDir ? (expanded ? <FolderOpenIcon size={14} /> : <Folder size={14} />) : <File size={14} />}
            </span>
            <span className="treeLabel">{item.name}</span>
          </button>

          {isDir && expanded ? (
            <div className="treeChildren">
              {loading ? (
                <div className="treeLoading" style={{ paddingLeft: 28 + depth * 14 }}>
                  Loading folder…
                </div>
              ) : (
                renderTree(children, depth + 1)
              )}
            </div>
          ) : null}
        </div>
      );
    });
  };

  return (
    <aside className="pane hybridExplorerPane">
      <div className="paneTitle">
        <div>
          <div className="paneEyebrow">Workspace</div>
          <div className="paneHeading">
            Files
            <span className="paneCounter">{explorerItems.length}</span>
          </div>
        </div>
        <button className="btn paneToggleBtn" onClick={onHide}>
          Hide
        </button>
      </div>

      <div className="consoleBody sidebarBody">
        <div className="explorerSummaryCard compact">
          <div className="explorerSummaryTitle">{projectLabel}</div>
          {selectedProject !== "." ? <div className="explorerSummaryMeta" title={selectedProject}>{selectedProject}</div> : null}
          {projectOptions.length > 0 ? (
            <label className="explorerProjectSelectWrap">
              <span>Saved projects</span>
              <select
                className="explorerProjectSelect"
                value={selectedProject}
                onChange={(event) => onSelectProject(event.target.value)}
              >
                {projectOptions.map((project) => (
                  <option key={project.root} value={project.root}>
                    {project.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>

        <div className="explorerToolbar">
          <button className="btn subtleBtn" onClick={() => void onRefresh()}>
            <RefreshCw size={14} />
            <span>Refresh</span>
          </button>
          <button className="btn subtleBtn" onClick={onNewFile}>
            <Plus size={14} />
            <span>New file</span>
          </button>
          <button className="btn subtleBtn" onClick={() => void onRestoreCheckpoint()}>
            <RotateCcw size={14} />
            <span>Restore</span>
          </button>
        </div>

        <div className="treeExplorer">
          {explorerItems.length === 0 ? (
            <div className="emptyState compactEmptyState">
              <div className="emptyStateTitle">No files yet</div>
              <div className="emptyStateText">Open a folder, create a file, or let Clara scaffold the project structure for you.</div>
            </div>
          ) : (
            renderTree(explorerItems)
          )}
        </div>
      </div>
    </aside>
  );
};
