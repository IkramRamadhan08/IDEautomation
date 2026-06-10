import type { ReactNode } from "react";
import { Moon, Sun } from "lucide-react";
import type { IdentityInfo } from "../../shared/types";
import type { AppTheme } from "../appDefaults";

type WorkspaceOnboardingProps = {
  identity: IdentityInfo | null;
  appTheme: AppTheme;
  hasVerifiedHostedAuth: boolean;
  uploadMode: boolean;
  newProjectModal: ReactNode;
  savedProjectsPanel: ReactNode;
  folderInput: ReactNode;
  onToggleTheme: () => void;
  onLogout: () => void;
  onPickWorkspace: () => void;
  onOpenNewProject: () => void;
};

export function WorkspaceOnboarding({
  identity,
  appTheme,
  hasVerifiedHostedAuth,
  uploadMode,
  newProjectModal,
  savedProjectsPanel,
  folderInput,
  onToggleTheme,
  onLogout,
  onPickWorkspace,
  onOpenNewProject,
}: WorkspaceOnboardingProps) {
  return (
    <div className="workspaceGateWrap workspaceSetupWrap" id="top">
      {newProjectModal}
      <header className="apporaNav workspaceSetupNav">
        <a className="splineBrandButton apporaBrand" href="#top" aria-label="Appora home">
          <span className="authBrandMark">A</span>
          <span>Appora</span>
        </a>
        <div className="workspaceSetupNavMeta">
          <span>{identity?.display_name || identity?.email || "Signed in"}</span>
        </div>
        <div className="apporaNavActions">
          <button className="apporaThemeToggle" type="button" onClick={onToggleTheme} title={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`} aria-label={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`}>
            {appTheme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>
          <button className="apporaNavCta" type="button" onClick={onLogout}>
            Logout
          </button>
        </div>
      </header>
      <main className="workspaceSetupMain">
        <div className="workspaceGateCard pane workspaceSetupCard">
          <div className="workspaceGateKicker">Appora Studio</div>
          <div className="workspaceGateTitle">Open a professional workspace</div>
          <div className="workspaceGateSubtitle">Mulai dari repo, project tersimpan, atau workspace kosong yang siap dipakai agent untuk plan, edit, test, dan preview.</div>
          <div className="workspaceGateFeatureGrid">
            <div className="gateFeatureCard">
              <div className="gateFeatureTitle">Import repo</div>
              <div className="gateFeatureText">Masuk dari codebase yang sudah ada tanpa buang struktur project.</div>
            </div>
            <div className="gateFeatureCard">
              <div className="gateFeatureTitle">Create app</div>
              <div className="gateFeatureText">Buka kanvas baru untuk alur agent dari prompt sampai preview.</div>
            </div>
            <div className="gateFeatureCard">
              <div className="gateFeatureTitle">Continue work</div>
              <div className="gateFeatureText">Ambil lagi project tersimpan dengan konteks dan file tetap dekat.</div>
            </div>
          </div>
          <div className="workspaceGateActions">
            <button className="btn primary" onClick={onPickWorkspace}>{uploadMode ? "Upload project..." : "Open project..."}</button>
            <button className="btn" onClick={onOpenNewProject}>New project</button>
          </div>
          <div className="workspaceGateTutorNote">Choose the project first. Appora keeps editor, agent trace, terminal, and preview in one flow.</div>
          {hasVerifiedHostedAuth ? savedProjectsPanel : null}
          {folderInput}
        </div>
      </main>
    </div>
  );
}
