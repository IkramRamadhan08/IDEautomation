import type { FormEvent, ReactNode } from "react";
import { Check, Copy, Download, Pencil, RefreshCw, Save, Trash2, X } from "lucide-react";
import type { HostedProject, ProjectTemplate } from "../../shared/api/client";

const fallbackTemplates: ProjectTemplate[] = [
  {
    id: "saas-dashboard",
    name: "SaaS Dashboard",
    category: "Dashboard",
    description: "Auth-ready dashboard starter.",
    best_for: "SaaS MVPs and portals.",
    tags: ["dashboard"],
  },
];

type NewProjectModalProps = {
  open: boolean;
  projectName: string;
  templates: ProjectTemplate[];
  selectedTemplateId: string;
  setupError: string;
  saving: boolean;
  authChecking: boolean;
  onClose: () => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onProjectNameChange: (value: string) => void;
  onTemplateChange: (templateId: string) => void;
  onContinueWithGoogle: () => void;
};

export function NewProjectModal({
  open,
  projectName,
  templates,
  selectedTemplateId,
  setupError,
  saving,
  authChecking,
  onClose,
  onSubmit,
  onProjectNameChange,
  onTemplateChange,
  onContinueWithGoogle,
}: NewProjectModalProps) {
  if (!open) return null;

  const displayedTemplates = templates.length > 0 ? templates : fallbackTemplates;

  return (
    <div className="modalBackdrop" onClick={() => !saving && onClose()}>
      <form className="newProjectModal pane" onSubmit={onSubmit} onClick={(event) => event.stopPropagation()}>
        <div className="newProjectModalHeader">
          <div>
            <div className="savedProjectsEyebrow">New project</div>
            <div className="newProjectModalTitle">Bikin project baru</div>
          </div>
          <button className="btn subtleBtn" type="button" disabled={saving} onClick={onClose}>
            Close
          </button>
        </div>
        <label className="newProjectField">
          <span>Project name</span>
          <input
            className="input"
            value={projectName}
            onChange={(event) => onProjectNameChange(event.target.value)}
            placeholder="Contoh: Portfolio Studio"
            disabled={saving || authChecking}
            autoFocus
          />
        </label>
        {setupError ? (
          <div className="projectSetupInlineError" role="alert">
            <span>{setupError}</span>
            <button className="btn subtleBtn" type="button" onClick={onContinueWithGoogle}>
              Continue with Google
            </button>
          </div>
        ) : null}
        <div className="newProjectTemplateSection">
          <div className="newProjectTemplateHeader">
            <span>Starter template</span>
            <small>{templates.length > 0 ? "Production-ready starting point" : "Loading templates..."}</small>
          </div>
          <div className="newProjectTemplateGrid">
            {displayedTemplates.map((template) => (
              <button
                key={template.id}
                className={`templateChoice ${selectedTemplateId === template.id ? "selected" : ""}`}
                type="button"
                disabled={saving || authChecking}
                onClick={() => onTemplateChange(template.id)}
              >
                <span className="templateChoiceTop">
                  <strong>{template.name}</strong>
                  <em>{template.category}</em>
                </span>
                <span className="templateChoiceDescription">{template.description}</span>
                <span className="templateChoiceBest">{template.best_for}</span>
              </button>
            ))}
          </div>
        </div>
        <div className="newProjectModalActions">
          <button className="btn" type="button" disabled={saving} onClick={onClose}>Cancel</button>
          <button className="btn primary" type="submit" disabled={!projectName.trim() || saving || authChecking}>
            {authChecking ? "Checking session..." : saving ? "Creating..." : "Create project"}
          </button>
        </div>
      </form>
    </div>
  );
}

type SavedProjectsPanelProps = {
  variant?: "setup" | "modal";
  projects: HostedProject[];
  selectedProject: string;
  setupError: string;
  refreshing: boolean;
  exporting: boolean;
  mutatingId: string;
  editingId: string;
  renameDraft: string;
  onRefresh: () => void;
  onOpenNewProject: () => void;
  onContinueWithGoogle: () => void;
  onDownloadProject: (projectRoot?: string) => void;
  onSaveProject: (projectRoot?: string) => void;
  onOpenProject: (projectRoot: string) => void;
  onDuplicateProject: (project: HostedProject) => void;
  onBeginRenameProject: (project: HostedProject) => void;
  onRenameDraftChange: (value: string) => void;
  onSaveRename: (project: HostedProject) => void;
  onCancelRename: () => void;
  onArchiveProject: (project: HostedProject) => void;
};

export function SavedProjectsPanel({
  variant = "setup",
  projects,
  selectedProject,
  setupError,
  refreshing,
  exporting,
  mutatingId,
  editingId,
  renameDraft,
  onRefresh,
  onOpenNewProject,
  onContinueWithGoogle,
  onDownloadProject,
  onSaveProject,
  onOpenProject,
  onDuplicateProject,
  onBeginRenameProject,
  onRenameDraftChange,
  onSaveRename,
  onCancelRename,
  onArchiveProject,
}: SavedProjectsPanelProps) {
  const firstProjectRoot = projects[0]?.root || ".";

  return (
    <div className={`savedProjectsPanel ${variant === "modal" ? "projectManagerPanel" : ""}`}>
      <div className="savedProjectsHeader">
        <div>
          <div className="savedProjectsEyebrow">Saved projects</div>
          <div className="savedProjectsTitle">Project yang pernah dibuat</div>
        </div>
        <div className="savedProjectsHeaderActions">
          <button className="btn subtleBtn" type="button" onClick={onRefresh} disabled={refreshing}>
            <RefreshCw size={14} className={refreshing ? "spinIcon" : ""} />
            <span>{refreshing ? "Refreshing" : "Refresh"}</span>
          </button>
          {variant === "modal" ? (
            <>
              <button className="btn subtleBtn" type="button" disabled={exporting || ((!selectedProject || selectedProject === ".") && projects.length === 0)} onClick={() => onDownloadProject(selectedProject && selectedProject !== "." ? selectedProject : firstProjectRoot)}>
                <Download size={14} className={exporting ? "spinIcon" : ""} />
                <span>{exporting ? "Saving" : "Save ZIP"}</span>
              </button>
              <button className="btn subtleBtn" type="button" disabled={!selectedProject || selectedProject === "." || Boolean(mutatingId)} onClick={() => onSaveProject()}>
                <Save size={14} className={mutatingId ? "spinIcon" : ""} />
                <span>Save</span>
              </button>
            </>
          ) : null}
          <button className="btn primary" type="button" onClick={onOpenNewProject}>
            New project
          </button>
        </div>
      </div>
      {setupError ? (
        <div className="projectSetupInlineError savedProjectsError" role="alert">
          <span>{setupError}</span>
          <button className="btn subtleBtn" type="button" onClick={onContinueWithGoogle}>
            Continue with Google
          </button>
        </div>
      ) : null}
      {projects.length > 0 ? (
        <div className="savedProjectsList">
          {projects.map((project) => {
            const editing = editingId === project.id;
            const busy = mutatingId === project.id;
            return (
              <div key={project.id} className={`savedProjectItem ${selectedProject === project.root ? "active" : ""}`}>
                <span className="savedProjectMain">
                  {editing ? (
                    <input
                      className="savedProjectRenameInput"
                      value={renameDraft}
                      onChange={(event) => onRenameDraftChange(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") onSaveRename(project);
                        if (event.key === "Escape") onCancelRename();
                      }}
                      autoFocus
                    />
                  ) : (
                    <>
                      <strong>{project.name}</strong>
                      <small>{project.root}</small>
                    </>
                  )}
                </span>
                <span className="savedProjectActions">
                  {editing ? (
                    <>
                      <button className="iconOnlyBtn" type="button" disabled={busy} onClick={() => onSaveRename(project)} title="Save rename">
                        <Check size={14} />
                      </button>
                      <button className="iconOnlyBtn" type="button" disabled={busy} onClick={onCancelRename} title="Cancel rename">
                        <X size={14} />
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="savedProjectOpen" type="button" disabled={busy} onClick={() => onOpenProject(project.root)}>
                        {variant === "setup" ? "Continue" : "Open"}
                      </button>
                      <button className="iconOnlyBtn" type="button" disabled={exporting} onClick={() => onDownloadProject(project.root)} title="Download ZIP">
                        <Download size={14} />
                      </button>
                      <button className="iconOnlyBtn" type="button" disabled={busy} onClick={() => onSaveProject(project.root)} title="Save hosted snapshot">
                        <Save size={14} />
                      </button>
                      <button className="iconOnlyBtn" type="button" disabled={busy} onClick={() => onDuplicateProject(project)} title="Duplicate project">
                        <Copy size={14} />
                      </button>
                      <button className="iconOnlyBtn" type="button" disabled={busy} onClick={() => onBeginRenameProject(project)} title="Rename project">
                        <Pencil size={14} />
                      </button>
                      <button className="iconOnlyBtn danger" type="button" disabled={busy} onClick={() => onArchiveProject(project)} title="Archive project">
                        <Trash2 size={14} />
                      </button>
                    </>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="emptyState compactEmptyState savedProjectEmpty">
          <div className="emptyStateTitle">Belum ada project tersimpan</div>
          <div className="emptyStateText">Pilih template lalu buat project baru. Project yang dibuat akan muncul di sini.</div>
        </div>
      )}
    </div>
  );
}

type ProjectManagerModalProps = {
  open: boolean;
  children: ReactNode;
  onClose: () => void;
};

export function ProjectManagerModal({ open, children, onClose }: ProjectManagerModalProps) {
  if (!open) return null;

  return (
    <div className="modalBackdrop" onClick={onClose}>
      <div className="newProjectModal projectManagerModal pane" onClick={(event) => event.stopPropagation()}>
        <div className="newProjectModalHeader">
          <div>
            <div className="savedProjectsEyebrow">Project manager</div>
            <div className="newProjectModalTitle">Kelola project</div>
          </div>
          <button className="btn subtleBtn" type="button" onClick={onClose}>
            Close
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
