import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import type { Session } from "@supabase/supabase-js";
import { Toaster, toast } from "sonner";
import { getCachedSupabaseAccessToken, supabase, supabaseConfigured } from "../shared/api/supabase";

import "./app.css";
import {
  detectProjects,
  getIdentity,
  getModels,
  getModelRouteDiagnostics,
  getSettings,
  getUserPreferences,
  getProjectPreferences,
  getWorkspace,
  listDir,
  readFile,
  pickWorkspaceNative,
  provisionWorkspace,
  resetClientIdentity,
  setWorkspace,
  testNineRouterRoute,
  updateSettings,
  updateUserPreferences,
  updateProjectPreferences,
  writeFile,
  fetchAgentCapabilities,
  importBrowserFolder,
  uploadImageAsset,
  listHostedProjects,
  listProjectTemplates,
  createHostedProject,
  renameHostedProject,
  archiveHostedProject,
  duplicateHostedProject,
  exportProjectZip,
  saveHostedProjectSnapshot,
  listCheckpoints,
  restoreCheckpoint,
  type HostedProject,
  type ProjectTemplate,
  type UserPreferences,
  type AgentCapabilities,
} from "../shared/api/client";

import {
  type ExplorerItem,
  type FileBuffer,
  type GoogleAuthStatus,
  type IdentityInfo,
  type ProviderChoice,
  type SettingsInfo,
  type SettingsUpdate,
  type BuildMode,
  type ProjectInfo,
  type HostedProject as HostedProjectType,
  type AgentAction,
  type UploadedImageAsset,
  type ModelRouteDiagnostics,
  type ModelRouteTestResult,
} from "../shared/types";

import { Topbar } from "../features/workspace/components/navigation/Topbar";
import { AgentOrb } from "../features/agent/components/AgentOrb";
import { runAgentWorkflow } from "../features/agent/workflow";
import { errorMessage, notifyToast } from "./feedback";
import { ensurePreviewRunningFlow, isHostedBrowser } from "../features/preview/runtime";
import { ApporaLoading } from "./components/ApporaLoading";
import { LandingGate } from "./components/LandingGate";
import { NewProjectModal, ProjectManagerModal, SavedProjectsPanel } from "./components/ProjectModals";
import { WorkspaceOnboarding } from "./components/WorkspaceOnboarding";
import { localDevUser, modelFromSettings } from "./appDefaults";
import { useAgentLiveFeed } from "./hooks/useAgentLiveFeed";
import { useAppTheme } from "./hooks/useAppTheme";
import { useAssistPaneResize } from "./hooks/useAssistPaneResize";
import { useRoutePath } from "./hooks/useRoutePath";

const SettingsModal = lazy(() => import("../features/settings/components/SettingsModal").then((module) => ({ default: module.SettingsModal })));
const HybridWorkspace = lazy(() => import("../features/workspace/modes/HybridWorkspace").then((module) => ({ default: module.HybridWorkspace })));
const FullAgentWorkspace = lazy(() => import("../features/workspace/modes/FullAgentWorkspace").then((module) => ({ default: module.FullAgentWorkspace })));
const localDevAuthEnabled = Boolean(import.meta.env.DEV && !supabaseConfigured);

export default function App() {
  const [ws, setWs] = useState<string | null>(null);
  const [identity, setIdentity] = useState<IdentityInfo | null>(null);
  const [googleAuth, setGoogleAuth] = useState<GoogleAuthStatus | null>(null);
  const [googleAuthLoading, setGoogleAuthLoading] = useState(true);

  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [hostedProjects, setHostedProjects] = useState<HostedProjectType[]>([]);
  const [selectedProject, setSelectedProject] = useState<string>(".");
  const [workspaceSetupComplete, setWorkspaceSetupComplete] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string>("");
  const [previewFrameKey, setPreviewFrameKey] = useState(0);

  const [agentInput, setAgentInput] = useState<string>("");
  const [agentStatus, setAgentStatus] = useState<"idle" | "thinking" | "error">("idle");
  const [, setAgentLog] = useState<string>("");
  const [agentReply, setAgentReply] = useState<string>("");
  const [agentActions, setAgentActions] = useState<AgentAction[]>([]);
  const [agentWidgetOpen, setAgentWidgetOpen] = useState(false);
  const [attachedImage, setAttachedImage] = useState<UploadedImageAsset | null>(null);
  const [imageUploading, setImageUploading] = useState(false);

  const [agentOrbPosition, setAgentOrbPosition] = useState<{ x: number; y: number } | null>(null);
  const [workingMsg, setWorkingMsg] = useState<string>("");
  const [, setAgentCapabilities] = useState<AgentCapabilities | null>(null);
  const {
    agentLiveItems,
    setAgentLiveItems,
    setAgentAuditTrail,
    agentRunViewPinned,
    setAgentRunViewPinned,
    makeAgentLiveId,
    pushAgentLiveItem,
    appendAssistantLiveText,
    resetAgentRunView,
  } = useAgentLiveFeed();

  const projectOptions = hostedProjects.length > 0
    ? hostedProjects.map((project) => ({ root: project.root, name: project.name }))
    : projects.map((project) => ({ root: project.root, name: project.name }));

  const [settingsOpen, setSettingsOpen] = useState(false);
  const { appTheme, toggleAppTheme } = useAppTheme();
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [projectManagerOpen, setProjectManagerOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [projectTemplates, setProjectTemplates] = useState<ProjectTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState("saas-dashboard");
  const [newProjectSaving, setNewProjectSaving] = useState(false);
  const [projectAuthChecking, setProjectAuthChecking] = useState(false);
  const [projectSetupError, setProjectSetupError] = useState("");
  const [projectsRefreshing, setProjectsRefreshing] = useState(false);
  const [projectMutatingId, setProjectMutatingId] = useState<string>("");
  const [projectExporting, setProjectExporting] = useState(false);
  const [projectEditingId, setProjectEditingId] = useState<string>("");
  const [projectRenameDraft, setProjectRenameDraft] = useState("");
  const [settings, setSettings] = useState<SettingsInfo | null>(null);
  const [buildMode, setBuildMode] = useState<BuildMode>("hybrid");
  const [buildModeDraft, setBuildModeDraft] = useState<BuildMode>("hybrid");
  const [agentAccessModeDraft, setAgentAccessModeDraft] = useState<"safe" | "trusted">("safe");
  const [modelDraft, setModelDraft] = useState<string>("");
  const [nineRouterBaseUrlDraft, setNineRouterBaseUrlDraft] = useState<string>("http://127.0.0.1:20128/v1");
  const [nineRouterApiKeyDraft, setNineRouterApiKeyDraft] = useState<string>("");
  const [models, setModels] = useState<string[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string>("");
  const [modelRouteDiagnostics, setModelRouteDiagnostics] = useState<ModelRouteDiagnostics | null>(null);
  const [modelRouteLoading, setModelRouteLoading] = useState(false);
  const [modelRouteTest, setModelRouteTest] = useState<ModelRouteTestResult | null>(null);
  const [modelRouteTesting, setModelRouteTesting] = useState(false);

  const [explorerItems, setExplorerItems] = useState<ExplorerItem[]>([]);
  const [treeExpanded, setTreeExpanded] = useState<Record<string, boolean>>({});
  const [treeChildren, setTreeChildren] = useState<Record<string, ExplorerItem[]>>({});
  const [treeLoading, setTreeLoading] = useState<Record<string, boolean>>({});
  const [openFiles, setOpenFiles] = useState<string[]>([]);
  const [activeFile, setActiveFile] = useState<string>("");
  const [buffers, setBuffers] = useState<Record<string, FileBuffer>>({});
  const [editorStatus, setEditorStatus] = useState<string>("Ready");
  const [editorBusy, setEditorBusy] = useState(false);
  const [showExplorerPane, setShowExplorerPane] = useState(true);
  const [showAssistPane, setShowAssistPane] = useState(true);
  const { assistPaneWidth, isResizingAssistPane, startAssistPaneResize } = useAssistPaneResize();
  const { routePath, navigateTo } = useRoutePath();

  const modelFromPreferences = (provider: ProviderChoice, prefs: UserPreferences | null): string => {
    if (!prefs) return "";
    if (provider === "nine_router") return prefs.nine_router_model || "free-forever";
    if (provider === "openai") return prefs.openai_model || "";
    if (provider === "anthropic") return prefs.anthropic_model || "";
    if (provider === "openrouter") return prefs.openrouter_model || "";
    if (provider === "groq") return prefs.groq_model || "";
    if (provider === "gemini") return prefs.gemini_model || "";
    if (provider === "together") return prefs.together_model || "";
    if (provider === "cerebras") return prefs.cerebras_model || "";
    if (provider === "xai") return prefs.xai_model || "";
    return "";
  };
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const hasVerifiedHostedAuth = Boolean(settings?.supabase_enabled && googleAuth?.authenticated);
  const authUserKey = googleAuth?.authenticated
    ? googleAuth.user?.sub || googleAuth.user?.email || "authenticated"
    : "";
  const activeAuthUserRef = useRef<string | null>(null);
  const latestAuthUserKeyRef = useRef(authUserKey);
  latestAuthUserKeyRef.current = authUserKey;

  const clearWorkspaceUiState = useCallback(() => {
    setWs(null);
    setIdentity(null);
    setProjects([]);
    setHostedProjects([]);
    setSelectedProject(".");
    setWorkspaceSetupComplete(false);
    setPreviewUrl("");
    setPreviewFrameKey((prev) => prev + 1);
    setAgentInput("");
    setAgentStatus("idle");
    setAgentLog("");
    setAgentReply("");
    setAgentActions([]);
    setAttachedImage(null);
    setWorkingMsg("");
    setAgentCapabilities(null);
    setExplorerItems([]);
    setTreeExpanded({});
    setTreeChildren({});
    setTreeLoading({});
    setOpenFiles([]);
    setActiveFile("");
    setBuffers({});
    setEditorStatus("Ready");
    setEditorBusy(false);
    setProjectSetupError("");
    resetAgentRunView();
  }, [resetAgentRunView]);

  const bindFolderInputRef = (node: HTMLInputElement | null) => {
    folderInputRef.current = node;
    if (node) {
      node.setAttribute("webkitdirectory", "");
      node.setAttribute("directory", "");
    }
  };

  const renderFolderInput = () => (
    <input
      ref={bindFolderInputRef}
      type="file"
      multiple
      style={{ display: "none" }}
      onChange={e => importPickedFolder(e.target.files)}
    />
  );

  // --- Auth & Init ---
  useEffect(() => {
    let mounted = true;
    if (typeof window !== "undefined") {
      window.localStorage.removeItem("voiceide-demo-mode");
    }

    const applySessionAuth = (session: Session | null) => {
      if (session) {
        setProjectSetupError("");
        setGoogleAuth({
          ok: true,
          authenticated: true,
          phase: "done",
          user: {
            sub: session.user.id,
            email: session.user.email ?? null,
            name: typeof session.user.user_metadata?.full_name === "string" ? session.user.user_metadata.full_name : null,
            picture: typeof session.user.user_metadata?.avatar_url === "string" ? session.user.user_metadata.avatar_url : null,
          },
        });
        return;
      }
      setGoogleAuth({ ok: true, authenticated: false, phase: "idle", user: null });
    };

    const minimumLoader = new Promise((resolve) => window.setTimeout(resolve, 850));
    if (localDevAuthEnabled) {
      setProjectSetupError("");
      setGoogleAuth(localDevUser());
      minimumLoader.then(() => {
        if (mounted) setGoogleAuthLoading(false);
      });
      return () => {
        mounted = false;
      };
    }

    supabase.auth.getSession().then(async ({ data: { session } }) => {
      applySessionAuth(session);
      await minimumLoader;
      if (mounted) setGoogleAuthLoading(false);
    });

    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
      applySessionAuth(session);
    });

    return () => {
      mounted = false;
      subscription.unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (googleAuth?.authenticated) {
      void loadIdentityOverview();
      void loadSettingsOverview();
      void loadWorkspaceOverview();
      return;
    }
    clearWorkspaceUiState();
  }, [clearWorkspaceUiState, googleAuth?.authenticated, googleAuth?.phase]);

  useEffect(() => {
    const previousAuthUser = activeAuthUserRef.current;
    if (previousAuthUser === authUserKey) return;

    activeAuthUserRef.current = authUserKey;
    if (previousAuthUser === null) return;

    resetClientIdentity();
    clearWorkspaceUiState();
  }, [authUserKey, clearWorkspaceUiState]);

  const loadSettingsOverview = async () => {
    const requestAuthUserKey = latestAuthUserKeyRef.current;
    try {
      const s = await getSettings();
      if (requestAuthUserKey !== latestAuthUserKeyRef.current) return;
      setSettings(s);

      let nextBuildMode = s.build_mode || "hybrid";
      let nextProvider = (s.llm_provider || "") as ProviderChoice;
      let nextModel = "";

      if (s.supabase_enabled && googleAuth?.authenticated) {
        try {
          const prefRes = await getUserPreferences();
          if (requestAuthUserKey !== latestAuthUserKeyRef.current) return;
          const prefs = prefRes.preferences;
          nextBuildMode = prefs.build_mode || nextBuildMode;
          nextProvider = "nine_router";
          nextModel = modelFromPreferences(nextProvider, prefs) || modelFromSettings(nextProvider, s);
        } catch {
          // ignore hosted preference load failures and keep global settings fallback
        }
      }

      setBuildMode(nextBuildMode);
      setBuildModeDraft(nextBuildMode);
      nextProvider = "nine_router";
      setModelDraft(nextModel || modelFromSettings(nextProvider, s));
      setNineRouterBaseUrlDraft(s.nine_router_base_url || "http://127.0.0.1:20128/v1");
    } catch { /* ignore */ }
  };

  const loadIdentityOverview = async () => {
    const requestAuthUserKey = latestAuthUserKeyRef.current;
    try {
      const info = await getIdentity();
      if (requestAuthUserKey !== latestAuthUserKeyRef.current) return;
      setIdentity(info);
    } catch { /* ignore */ }
  };

  const loadWorkspaceOverview = async () => {
    const requestAuthUserKey = latestAuthUserKeyRef.current;
    try {
      const info = await getWorkspace();
      if (requestAuthUserKey !== latestAuthUserKeyRef.current) return;
      if (info.path) {
        setWs(info.path);
        setWorkspaceSetupComplete(false);
        return;
      }
      if (isHostedBrowser() || localDevAuthEnabled) {
        const provisioned = await provisionWorkspace();
        if (requestAuthUserKey !== latestAuthUserKeyRef.current) return;
        setWs(provisioned.path);
        setWorkspaceSetupComplete(false);
      }
    } catch {
      if (!isHostedBrowser() && !localDevAuthEnabled) return;
      try {
        const provisioned = await provisionWorkspace();
        if (requestAuthUserKey !== latestAuthUserKeyRef.current) return;
        setWs(provisioned.path);
        setWorkspaceSetupComplete(false);
      } catch {
        // keep the current workspace state if a background restore/provision check fails
      }
    }
  };

  const startGoogleLogin = async (redirectPath = "/app") => {
    try {
      const { error } = await supabase.auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: `${window.location.origin}${redirectPath}` },
      });
      if (error) throw error;
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const openAppora = () => {
    if (googleAuth?.authenticated) {
      navigateTo("/app");
      return;
    }
    if (localDevAuthEnabled) {
      setProjectSetupError("");
      setGoogleAuth(localDevUser());
      setGoogleAuthLoading(false);
      navigateTo("/app");
      return;
    }
    void startGoogleLogin("/app");
  };

  const logoutToStart = async () => {
    if (!localDevAuthEnabled) {
      try {
        await supabase.auth.signOut();
      } catch {
        // ignore sign-out transport errors and clear local state anyway
      }
    }
    resetClientIdentity();
    clearWorkspaceUiState();
    setGoogleAuth({ ok: true, authenticated: false, phase: "idle", user: null });
  };

  // --- Workspace & Files ---
  const hostedSessionMessage = (actionLabel: string) =>
    `Sesi login Appora belum valid untuk ${actionLabel}. Klik Continue with Google lagi, lalu ulangi.`;

  const ensureHostedSession = async (actionLabel: string): Promise<Session | null> => {
    if (!settings?.supabase_enabled) return null;
    setProjectAuthChecking(true);
    try {
      let session: Session | null = null;
      let error: unknown = null;

      try {
        const result = await supabase.auth.getSession();
        session = result.data.session;
        error = result.error;
      } catch (err) {
        error = err;
      }

      if (session?.access_token) return session;
      if (getCachedSupabaseAccessToken()) return null;

      if (error || !session?.access_token) {
        try {
          const refreshed = await supabase.auth.refreshSession();
          session = refreshed.data.session;
          if (session?.access_token) return session;
          if (getCachedSupabaseAccessToken()) return null;
        } catch {
          if (getCachedSupabaseAccessToken()) return null;
        }
      }

      if (!session?.access_token) {
        const message = hostedSessionMessage(actionLabel);
        setProjectSetupError(message);
        setEditorStatus("Login required");
        throw new Error(message);
      }

      return session;
    } catch (error) {
      const message = error instanceof Error && error.message.includes("Sesi login Appora")
        ? error.message
        : hostedSessionMessage(actionLabel);
      setProjectSetupError(message);
      setEditorStatus("Login required");
      throw new Error(message);
    } finally {
      setProjectAuthChecking(false);
    }
  };

  const handleProjectActionError = (prefix: string, error: unknown) => {
    const message = errorMessage(error);
    setProjectSetupError(message);
    toast.error(`${prefix}: ${message}`);
  };

  const refreshProjects = async () => {
    const requestAuthUserKey = latestAuthUserKeyRef.current;
    setProjectsRefreshing(true);
    try {
      const [detected, hosted, templates] = await Promise.all([
        detectProjects().catch(() => ({ ok: true, projects: [] as ProjectInfo[] })),
        hasVerifiedHostedAuth ? listHostedProjects().catch((error) => ({ error })) : Promise.resolve({ ok: true, projects: [] as HostedProject[] }),
        listProjectTemplates().catch((error) => ({ error })),
      ]);
      if (requestAuthUserKey !== latestAuthUserKeyRef.current) return;
      setProjects(detected.projects || []);
      if ("error" in hosted) {
        setProjectSetupError(errorMessage(hosted.error));
      } else {
        setHostedProjects(hosted.projects || []);
      }
      if ("error" in templates) {
        setProjectSetupError(errorMessage(templates.error));
      } else {
        setProjectTemplates(templates.templates || []);
      }
    } catch {
      if (requestAuthUserKey !== latestAuthUserKeyRef.current) return;
      setProjects([]);
      setHostedProjects([]);
      setProjectTemplates([]);
    } finally {
      setProjectsRefreshing(false);
    }
  };

  const refreshExplorer = async (path = selectedProject !== "." ? selectedProject : ".") => {
    setTreeLoading((prev) => ({ ...prev, [path]: true }));
    try {
      const res = await listDir(path);
      if (!res || !res.items) {
        if (path === ".") setExplorerItems([]);
        setTreeChildren((prev) => ({ ...prev, [path]: [] }));
        return;
      }
      const items = res.items
        .filter((item) => !item.name.startsWith("."))
        .sort((a, b) => {
          if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
          return a.name.localeCompare(b.name);
        });
      if (path === "." || path === selectedProject) setExplorerItems(items);
      setTreeChildren((prev) => ({ ...prev, [path]: items }));
    } catch (e) {
      console.error("Failed to refresh explorer", e);
      if (path === ".") setExplorerItems([]);
    } finally {
      setTreeLoading((prev) => ({ ...prev, [path]: false }));
    }
  };

  useEffect(() => {
    if (ws) {
      void refreshProjects();
    }
  }, [ws]);

  useEffect(() => {
    if (hasVerifiedHostedAuth) {
      void refreshProjects();
    }
  }, [hasVerifiedHostedAuth]);

  useEffect(() => {
    if (!workspaceSetupComplete) return;
    if (selectedProject !== ".") return;
    if (hostedProjects.length > 0) {
      setSelectedProject(hostedProjects[0].root);
      return;
    }
    if (projects.length > 0) {
      setSelectedProject(projects[0].root);
    }
  }, [hostedProjects, projects, selectedProject, workspaceSetupComplete]);

  useEffect(() => {
    setAttachedImage(null);
    setTreeExpanded({});
    setTreeChildren({});
    if (ws) {
      void refreshExplorer(selectedProject !== "." ? selectedProject : ".");
    }
  }, [selectedProject, ws]);

  useEffect(() => {
    if (!ws) {
      setAgentCapabilities(null);
      return;
    }
    let cancelled = false;
    const projectRoot = selectedProject || ".";
    fetchAgentCapabilities(projectRoot, false)
      .then((caps) => {
        if (!cancelled) setAgentCapabilities(caps);
      })
      .catch(() => {
        if (!cancelled) setAgentCapabilities(null);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedProject, ws]);

  useEffect(() => {
    const loadSelectedProjectPrefs = async () => {
      if (!selectedProject || selectedProject === "." || !hasVerifiedHostedAuth) {
        setAgentAccessModeDraft("safe");
        return;
      }
      const hosted = hostedProjects.find((project) => project.root === selectedProject);
      if (!hosted) {
        setAgentAccessModeDraft("safe");
        return;
      }
      try {
        const prefRes = await getProjectPreferences(hosted.id);
        const prefs = prefRes.preferences;
        if (prefs.build_mode) {
          setBuildMode(prefs.build_mode);
          setBuildModeDraft(prefs.build_mode);
        }
        const accessMode = prefs.agent_access_mode === "trusted" ? "trusted" : "safe";
        setAgentAccessModeDraft(accessMode);
      } catch {
        // ignore project pref load failures during trial mode
      }
    };

    void loadSelectedProjectPrefs();
  }, [selectedProject, hostedProjects, hasVerifiedHostedAuth]);

  const toggleTreeDir = async (path: string) => {
    const nextExpanded = !treeExpanded[path];
    setTreeExpanded((prev) => ({ ...prev, [path]: nextExpanded }));
    if (nextExpanded) {
      if (treeChildren[path]) return;
      await refreshExplorer(path);
    }
  };

  const selectProject = (project: string) => {
    setSelectedProject(project);
    setActiveFile("");
    setOpenFiles([]);
    setBuffers({});
    setPreviewUrl("");
    setPreviewFrameKey((value) => value + 1);
  };

  const ensureWorkspaceReady = async () => {
    if (ws) return ws;
    const provisioned = await provisionWorkspace();
    setWs(provisioned.path);
    return provisioned.path;
  };

  const loadProjectIntoWorkspace = async (projectRoot: string) => {
    selectProject(projectRoot);
    setWorkspaceSetupComplete(true);
    try {
      const res = await listDir(projectRoot);
      const items = (res.items || [])
        .filter((item) => !item.name.startsWith("."))
        .sort((a, b) => {
          if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
          return a.name.localeCompare(b.name);
        });
      setExplorerItems(items);
      setTreeChildren((prev) => ({ ...prev, [projectRoot]: items }));
      setTreeExpanded((prev) => ({ ...prev, [projectRoot]: true }));
    } catch (e) {
      setExplorerItems([]);
      setTreeChildren((prev) => ({ ...prev, [projectRoot]: [] }));
      setEditorStatus(`Project selected, but file tree failed to load: ${errorMessage(e)}`);
    }
  };

  const openSavedProject = async (projectRoot: string) => {
    setProjectSetupError("");
    try {
      setEditorStatus(`Opening project ${projectRoot}...`);
      await ensureHostedSession("membuka project");
      await ensureWorkspaceReady();
      await loadProjectIntoWorkspace(projectRoot);
      setProjectManagerOpen(false);
      setEditorStatus(`Project ready: ${projectRoot}`);
      toast.success("Project dibuka");
    } catch (e) {
      handleProjectActionError("Gagal membuka project", e);
    }
  };

  const beginRenameProject = (project: HostedProjectType) => {
    setProjectEditingId(project.id);
    setProjectRenameDraft(project.name);
  };

  const saveProjectRename = async (project: HostedProjectType) => {
    const name = projectRenameDraft.trim();
    if (!name || projectMutatingId) return;
    setProjectMutatingId(project.id);
    setProjectSetupError("");
    try {
      await ensureHostedSession("rename project");
      const res = await renameHostedProject(project.id, { name });
      setHostedProjects((prev) => prev.map((item) => item.id === project.id ? res.project : item));
      setProjectEditingId("");
      setProjectRenameDraft("");
      toast.success("Project renamed");
    } catch (e) {
      handleProjectActionError("Gagal rename project", e);
    } finally {
      setProjectMutatingId("");
    }
  };

  const archiveProjectFromList = async (project: HostedProjectType) => {
    setProjectMutatingId(project.id);
    setProjectSetupError("");
    try {
      await ensureHostedSession("menghapus project");
      await archiveHostedProject(project.id);
      const remaining = hostedProjects.filter((item) => item.id !== project.id);
      setHostedProjects(remaining);
      if (selectedProject === project.root) {
        const next = remaining[0]?.root || ".";
        if (next !== ".") {
          await loadProjectIntoWorkspace(next);
        } else {
          selectProject(".");
          setExplorerItems([]);
          setWorkspaceSetupComplete(false);
        }
      }
      void refreshProjects();
      toast.success("Project archived");
    } catch (e) {
      handleProjectActionError("Gagal archive project", e);
    } finally {
      setProjectMutatingId("");
    }
  };

  const duplicateProjectFromList = async (project: HostedProjectType) => {
    setProjectMutatingId(project.id);
    setProjectSetupError("");
    try {
      await ensureHostedSession("duplicate project");
      const res = await duplicateHostedProject(project.id, { name: `${project.name} Copy` });
      setHostedProjects((prev) => [res.project, ...prev.filter((item) => item.id !== res.project.id)]);
      await loadProjectIntoWorkspace(res.project.root);
      void refreshProjects();
      toast.success(`Project duplicated: ${res.project.name}`);
    } catch (e) {
      handleProjectActionError("Gagal duplicate project", e);
    } finally {
      setProjectMutatingId("");
    }
  };

  const saveCurrentHostedProject = async (projectRoot = selectedProject) => {
    const hosted = hostedProjects.find((project) => project.root === projectRoot);
    if (!hosted) {
      toast.error("Pilih project tersimpan dulu sebelum save");
      return;
    }
    setProjectMutatingId(hosted.id);
    setProjectSetupError("");
    try {
      await ensureHostedSession("save project");
      if (activeFile && buffers[activeFile]?.dirty) {
        await writeFile(activeFile, buffers[activeFile].content);
        setBuffers((prev) => ({ ...prev, [activeFile]: { ...prev[activeFile], dirty: false } }));
      }
      const res = await saveHostedProjectSnapshot(hosted.id);
      setHostedProjects((prev) => prev.map((item) => item.id === hosted.id ? res.project : item));
      setEditorStatus(`Project saved: ${res.project.name}`);
      toast.success("Project saved to Appora");
    } catch (e) {
      handleProjectActionError("Gagal save project", e);
    } finally {
      setProjectMutatingId("");
    }
  };

  const downloadProjectToDevice = async (projectRoot = selectedProject) => {
    const target = (projectRoot || selectedProject || ".").trim();
    if (!target || target === ".") {
      toast.error("Pilih project dulu sebelum download");
      return;
    }
    setProjectExporting(true);
    setProjectSetupError("");
    setEditorStatus(`Preparing ZIP for ${target}...`);
    try {
      await ensureHostedSession("download ZIP");
      await ensureWorkspaceReady();
      const exported = await exportProjectZip(target);
      const url = URL.createObjectURL(exported.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = exported.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setEditorStatus(`Saved ZIP: ${exported.filename}`);
      toast.success(`Project disimpan: ${exported.filename}`);
    } catch (e) {
      setEditorStatus("Failed to save project ZIP");
      handleProjectActionError("Gagal download project", e);
    } finally {
      setProjectExporting(false);
    }
  };

  const restoreLatestCheckpoint = async () => {
    try {
      const res = await listCheckpoints(selectedProject);
      const latest = res.items[0];
      if (!latest) {
        toast.info("Belum ada checkpoint untuk project ini");
        return;
      }
      const restored = await restoreCheckpoint(latest.path);
      setBuffers({});
      setActiveFile("");
      setOpenFiles([]);
      await refreshExplorer(selectedProject !== "." ? selectedProject : ".");
      setEditorStatus(`Restored checkpoint: ${latest.name}`);
      toast.success(`Checkpoint dipulihkan: ${restored.restored} file`);
    } catch (e) {
      toast.error("Gagal restore checkpoint: " + errorMessage(e));
    }
  };

  const openFile = async (path: string) => {
    setActiveFile(path);
    setEditorStatus(`Opening ${path}...`);
    if (!openFiles.includes(path)) setOpenFiles((v) => [...v, path]);
    if (buffers[path]) {
      setEditorStatus(`Loaded ${path}`);
      return;
    }
    setEditorBusy(true);
    try {
      const res = await readFile(path);
      setBuffers((prev) => ({ ...prev, [path]: { content: res.content, dirty: false } }));
      setEditorStatus(`Loaded ${path}`);
    } catch (e) {
      setEditorStatus("Failed to open file");
      toast.error("Gagal membuka file: " + errorMessage(e));
    } finally {
      setEditorBusy(false);
    }
  };

  const saveFile = async () => {
    if (!activeFile || !buffers[activeFile]) return;
    setEditorBusy(true);
    setEditorStatus(`Saving ${activeFile}...`);
    try {
      await writeFile(activeFile, buffers[activeFile].content);
      setBuffers((prev) => ({ ...prev, [activeFile]: { ...prev[activeFile], dirty: false } }));
      setEditorStatus(`Saved ${activeFile}`);
      toast.success("File disimpan");
    } catch (e) {
      setEditorStatus("Failed to save file");
      toast.error("Gagal menyimpan file: " + errorMessage(e));
    } finally {
      setEditorBusy(false);
    }
  };

  const closeFile = (path: string) => {
    setOpenFiles((prev) => prev.filter((p) => p !== path));
    if (activeFile === path) setActiveFile("");
  };

  const pickWorkspace = async () => {
    if (isHostedBrowser()) {
      folderInputRef.current?.click();
      return;
    }
    try {
      const picked = await pickWorkspaceNative();
      if (picked?.ok && picked.path) {
        const res = await setWorkspace(picked.path);
        setWs(res.path);
        setWorkspaceSetupComplete(true);
        return;
      }
    } catch {
      folderInputRef.current?.click();
    }
  };

  const createHostedProjectFromName = async (event?: FormEvent) => {
    event?.preventDefault();
    const name = newProjectName.trim();
    if (!name || newProjectSaving) return;
    setNewProjectSaving(true);
    setProjectSetupError("");
    setEditorStatus(`Creating project ${name}...`);
    try {
      await ensureHostedSession("membuat project");

      const res = await createHostedProject({ name, template_id: selectedTemplateId || "blank" });
      if (hasVerifiedHostedAuth) {
        updateProjectPreferences(res.project.id, { build_mode: buildModeDraft, agent_access_mode: agentAccessModeDraft }).catch(() => {
          // Project creation must not be blocked by optional preference persistence.
        });
      }
      setHostedProjects((prev) => [res.project, ...prev.filter((project) => project.id !== res.project.id)]);
      await loadProjectIntoWorkspace(res.project.root);
      void refreshProjects();
      setEditorStatus(`Project ready: ${res.project.name}`);
      setNewProjectName("");
      setSelectedTemplateId("saas-dashboard");
      setNewProjectOpen(false);
      toast.success(`Project created: ${res.project.name}`);
    } catch (e) {
      handleProjectActionError("Gagal membuat project", e);
    } finally {
      setNewProjectSaving(false);
    }
  };

  const importPickedFolder = async (fileList: FileList | null) => {
    const files = fileList ? Array.from(fileList) : [];
    if (files.length === 0) return;
    setEditorStatus("Importing workspace folder...");
    try {
      const res = await importBrowserFolder(files);
      setWs(res.path);
      setWorkspaceSetupComplete(true);
      setEditorStatus(`Workspace imported: ${res.path}`);
    } catch (e) {
      setEditorStatus("Failed to import workspace folder");
      toast.error("Gagal import folder: " + errorMessage(e));
    }
  };

  const createNewFile = async () => {
    const name = window.prompt("Nama file baru:");
    if (!name) return;
    const targetPath = selectedProject !== "." ? `${selectedProject}/${name}` : name;
    setEditorStatus(`Creating ${targetPath}...`);
    try {
      await writeFile(targetPath, "");
      await refreshExplorer();
      await openFile(targetPath);
      setEditorStatus(`Created ${targetPath}`);
      toast.success("File dibuat");
    } catch (e) {
      setEditorStatus("Failed to create file");
      toast.error("Gagal membuat file: " + errorMessage(e));
    }
  };

  const pickAgentImage = () => {
    imageInputRef.current?.click();
  };

  const normalizeImageAlias = (value: string, fallback: string) => {
    const clean = value
      .trim()
      .replace(/^@+/, "")
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
    return clean || fallback;
  };

  const defaultImageAlias = (file: File) => {
    const stem = file.name.replace(/\.[^.]+$/, "");
    if (/hero|banner|cover/i.test(stem)) return "hero";
    if (/logo|brand/i.test(stem)) return "logo";
    if (/product|produk/i.test(stem)) return "produk";
    return normalizeImageAlias(stem, "image");
  };

  const importAgentImage = async (fileList: FileList | null) => {
    const file = fileList?.[0];
    if (!file) return;
    const suggestedAlias = defaultImageAlias(file);
    const titleInput = window.prompt(
      "Kasih title/alias buat gambar ini. Nanti bisa dipanggil di prompt, contoh: @hero",
      suggestedAlias,
    );
    const alias = normalizeImageAlias(titleInput ?? suggestedAlias, suggestedAlias);
    const title = alias;
    setImageUploading(true);
    try {
      const uploaded = await uploadImageAsset(selectedProject, file, title);
      setAttachedImage({ ...uploaded, title, alias: uploaded.alias || alias });
      setAgentInput((current) => {
        const token = `@${uploaded.alias || alias}`;
        if (current.includes(token)) return current;
        return current.trim() ? `${current.trim()} ${token} ` : `${token} `;
      });
      toast.success(`Image attached as @${uploaded.alias || alias}`);
    } catch (e) {
      toast.error("Gagal upload image: " + errorMessage(e));
    } finally {
      setImageUploading(false);
    }
  };

  // --- Settings ---
  const openSettings = async () => {
    setSettingsOpen(true);
    await loadProviderModels("nine_router");
  };

  const loadProviderModels = async (p: ProviderChoice) => {
    if (!p) {
      setModels([]);
      setModelsError("");
      return;
    }
    setModelsLoading(true);
    setModelsError("");
    try {
      const res = await getModels(p);
      const nextModels = Array.from(new Set(res.models || []));
      setModels(nextModels);
      setModelDraft((current) => {
        if (current && nextModels.includes(current)) return current;
        const fallback = modelFromSettings(p, settings);
        if (fallback && nextModels.includes(fallback)) return fallback;
        return current || fallback || nextModels[0] || "";
      });
    } catch (e) {
      setModelsError(errorMessage(e));
    } finally {
      setModelsLoading(false);
    }
  };

  useEffect(() => {
    if (!settingsOpen || !modelDraft.trim()) {
      setModelRouteDiagnostics(null);
      return;
    }
    let cancelled = false;
    setModelRouteLoading(true);
    getModelRouteDiagnostics("nine_router", modelDraft.trim())
      .then((diagnostics) => {
        if (!cancelled) setModelRouteDiagnostics(diagnostics);
      })
      .catch(() => {
        if (!cancelled) setModelRouteDiagnostics(null);
      })
      .finally(() => {
        if (!cancelled) setModelRouteLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [settingsOpen, modelDraft]);

  const testSelectedNineRouterRoute = async () => {
    setModelRouteTesting(true);
    setModelRouteTest(null);
    try {
      const result = await testNineRouterRoute({
        base_url: nineRouterBaseUrlDraft,
        api_key: nineRouterApiKeyDraft || null,
        model: modelDraft || "free-forever",
      });
      setModelRouteTest(result);
      if (result.ok) toast.success(result.summary);
      else toast.error(result.summary);
    } catch (e) {
      const result = {
        ok: false,
        status: 0,
        summary: "Gagal test 9Router: " + errorMessage(e),
        model: modelDraft || "free-forever",
      };
      setModelRouteTest(result);
      toast.error(result.summary);
    } finally {
      setModelRouteTesting(false);
    }
  };

  const saveSettings = async () => {
    try {
      const patch: SettingsUpdate = {
        llm_provider: "nine_router",
        build_mode: buildModeDraft,
        nine_router_base_url: nineRouterBaseUrlDraft,
      };
      if (modelDraft) {
        patch.nine_router_model = modelDraft;
      }
      if (nineRouterApiKeyDraft) patch.nine_router_api_key = nineRouterApiKeyDraft;

      await updateSettings(patch);

      if (hasVerifiedHostedAuth) {
        await updateUserPreferences({
          llm_provider: "nine_router",
          build_mode: buildModeDraft,
          openai_model: null,
          anthropic_model: null,
          nine_router_model: modelDraft,
          openrouter_model: null,
          groq_model: null,
          gemini_model: null,
          together_model: null,
          cerebras_model: null,
          xai_model: null,
        });
        const hosted = hostedProjects.find((project) => project.root === selectedProject);
        if (hosted) {
          await updateProjectPreferences(hosted.id, {
            build_mode: buildModeDraft,
            agent_access_mode: agentAccessModeDraft,
          });
        }
      }

      await loadSettingsOverview();
      setBuildMode(buildModeDraft);
      setSettingsOpen(false);
      toast.success("Settings disimpan");
    } catch (e) {
      toast.error("Gagal menyimpan settings: " + errorMessage(e));
    }
  };

  // --- Agent ---
  const runAgentAndAutoApply = async () => {
    const input = agentInput.trim();
    if (!input || agentStatus === "thinking") return;
    setAgentInput("");
    await runAgentWorkflow({
      agentInput: input,
      agentStatus,
      buildMode,
      friendlyFreeTierMode: settings?.friendly_free_tier_mode ?? true,
      previewUrl,
      selectedProject,
      attachedImagePath: attachedImage?.path || null,
      attachedImageAlias: attachedImage?.alias || attachedImage?.title || null,
      activeFile,
      openFiles,
      buffers,
      makeAgentLiveId,
      pushAgentLiveItem,
      appendAssistantLiveText,
      refreshExplorer,
      ensurePreviewRunning,
      refreshPreviewFrame: () => setPreviewFrameKey((value) => value + 1),
      notify: (payload) => notifyToast(toast, payload),
      errorMessage,
      setBuffers,
      setAgentStatus,
      setAgentWidgetOpen,
      setAgentRunViewPinned,
      setAgentReply,
      setAgentLog,
      setAgentActions,
      setAgentLiveItems,
      setAgentAuditTrail,
      setEditorStatus,
      setWorkingMsg,
    });
  };

  const ensurePreviewRunning = async () => ensurePreviewRunningFlow({
    workspacePath: ws,
    selectedProject,
    setEditorStatus,
    setPreviewUrl,
    refreshPreviewFrame: () => setPreviewFrameKey((value) => value + 1),
    notifyInfo: (message) => toast(message),
    notifyError: (message) => toast.error(message),
    errorMessage,
  });

  const quickSwitchBuildMode = (mode: BuildMode) => {
    setBuildMode(mode);
    setBuildModeDraft(mode);
    if (hasVerifiedHostedAuth) {
      void updateUserPreferences({ build_mode: mode });
    }
  };

  const openNewProjectModal = () => {
    setProjectSetupError("");
    setProjectManagerOpen(false);
    setNewProjectOpen(true);
  };

  // --- Renders ---
  const renderGoogleLoginGate = () => (
    <LandingGate
      appTheme={appTheme}
      projectSetupError={projectSetupError}
      onOpenAppora={openAppora}
      onToggleTheme={toggleAppTheme}
    />
  );

  const closeNewProjectModal = () => setNewProjectOpen(false);

  const continueWithGoogle = () => {
    void startGoogleLogin("/app");
  };

  const cancelProjectRename = () => {
    setProjectEditingId("");
    setProjectRenameDraft("");
  };

  const renderNewProjectModal = () => (
    <NewProjectModal
      open={newProjectOpen}
      projectName={newProjectName}
      templates={projectTemplates}
      selectedTemplateId={selectedTemplateId}
      setupError={projectSetupError}
      saving={newProjectSaving}
      authChecking={projectAuthChecking}
      onClose={closeNewProjectModal}
      onSubmit={createHostedProjectFromName}
      onProjectNameChange={(value) => {
        setNewProjectName(value);
        if (projectSetupError) setProjectSetupError("");
      }}
      onTemplateChange={setSelectedTemplateId}
      onContinueWithGoogle={continueWithGoogle}
    />
  );

  const renderSavedProjectsPanel = (variant: "setup" | "modal" = "setup") => (
    <SavedProjectsPanel
      variant={variant}
      projects={hostedProjects}
      selectedProject={selectedProject}
      setupError={projectSetupError}
      refreshing={projectsRefreshing}
      exporting={projectExporting}
      mutatingId={projectMutatingId}
      editingId={projectEditingId}
      renameDraft={projectRenameDraft}
      onRefresh={() => void refreshProjects()}
      onOpenNewProject={openNewProjectModal}
      onContinueWithGoogle={continueWithGoogle}
      onDownloadProject={(projectRoot) => void downloadProjectToDevice(projectRoot)}
      onSaveProject={(projectRoot) => void saveCurrentHostedProject(projectRoot)}
      onOpenProject={(projectRoot) => void openSavedProject(projectRoot)}
      onDuplicateProject={(project) => void duplicateProjectFromList(project)}
      onBeginRenameProject={beginRenameProject}
      onRenameDraftChange={setProjectRenameDraft}
      onSaveRename={(project) => void saveProjectRename(project)}
      onCancelRename={cancelProjectRename}
      onArchiveProject={(project) => void archiveProjectFromList(project)}
    />
  );

  const renderProjectManagerModal = () => (
    <ProjectManagerModal open={projectManagerOpen} onClose={() => setProjectManagerOpen(false)}>
      {renderSavedProjectsPanel("modal")}
    </ProjectManagerModal>
  );

  const renderWorkspaceOnboarding = () => (
    <WorkspaceOnboarding
      identity={identity}
      appTheme={appTheme}
      hasVerifiedHostedAuth={hasVerifiedHostedAuth}
      uploadMode={isHostedBrowser()}
      newProjectModal={renderNewProjectModal()}
      savedProjectsPanel={renderSavedProjectsPanel("setup")}
      folderInput={renderFolderInput()}
      onToggleTheme={toggleAppTheme}
      onLogout={logoutToStart}
      onPickWorkspace={pickWorkspace}
      onOpenNewProject={openNewProjectModal}
    />
  );

  if (googleAuthLoading) {
    return (
      <div className="workspaceGateWrap workspaceSetupWrap">
        <ApporaLoading title="Preparing Appora" subtitle="Checking your session and workspace state." />
      </div>
    );
  }

  if (routePath !== "/app") return renderGoogleLoginGate();
  if (!googleAuth?.authenticated) return renderGoogleLoginGate();
  if (!ws || !workspaceSetupComplete) return renderWorkspaceOnboarding();

  const renderHybridMode = () => (
    <HybridWorkspace
      ws={ws}
      selectedProject={selectedProject}
      projectOptions={projectOptions}
      explorerItems={explorerItems}
      treeExpanded={treeExpanded}
      treeChildren={treeChildren}
      treeLoading={treeLoading}
      activeFile={activeFile}
      openFiles={openFiles}
      buffers={buffers}
      editorBusy={editorBusy}
      agentStatus={agentStatus}
      editorStatus={editorStatus}
      showExplorerPane={showExplorerPane}
      showAssistPane={showAssistPane}
      assistPaneWidth={assistPaneWidth}
      isResizingAssistPane={isResizingAssistPane}
      previewUrl={previewUrl}
      previewFrameKey={previewFrameKey}
      recentActions={agentActions}
      agentLiveItems={agentLiveItems}
      onRefreshExplorer={refreshExplorer}
      onSelectProject={selectProject}
      onRestoreCheckpoint={restoreLatestCheckpoint}
      onToggleDir={toggleTreeDir}
      onOpenFile={openFile}
      onHideExplorer={() => setShowExplorerPane(false)}
      onNewFile={createNewFile}
      onSetActiveFile={setActiveFile}
      onCloseFile={closeFile}
      onRunInlineHelp={runAgentAndAutoApply}
      onSaveFile={saveFile}
      onBufferChange={(path, content) => setBuffers((p) => ({ ...p, [path]: { content, dirty: true } }))}
      onStartResizeAssistPane={startAssistPaneResize}
      onEnsurePreviewRunning={ensurePreviewRunning}
    />
  );

  const renderFullAgentMode = () => (
    <FullAgentWorkspace
      ws={ws}
      previewUrl={previewUrl}
      previewFrameKey={previewFrameKey}
      onEnsurePreviewRunning={ensurePreviewRunning}
    />
  );

  return (
    <div className={`shell appTheme-${appTheme}`}>
      <Toaster position="top-right" richColors />
      {hostedProjects.length > 0 ? null : null}
      <input ref={folderInputRef} type="file" multiple style={{ display: "none" }} onChange={e => importPickedFolder(e.target.files)} />
      <input
        ref={imageInputRef}
        type="file"
        accept="image/*"
        style={{ display: "none" }}
        onChange={e => {
          void importAgentImage(e.target.files);
          e.currentTarget.value = "";
        }}
      />
      
      <Suspense fallback={null}>
        <SettingsModal
          settingsOpen={settingsOpen}
          identity={identity}
          settings={settings}
          buildModeDraft={buildModeDraft}
          modelDraft={modelDraft}
          nineRouterBaseUrlDraft={nineRouterBaseUrlDraft}
          nineRouterApiKeyDraft={nineRouterApiKeyDraft}
          models={models}
          modelsLoading={modelsLoading}
          modelsError={modelsError}
          modelRouteDiagnostics={modelRouteDiagnostics}
          modelRouteLoading={modelRouteLoading}
          modelRouteTest={modelRouteTest}
          modelRouteTesting={modelRouteTesting}
          agentAccessModeDraft={agentAccessModeDraft}
          onClose={() => setSettingsOpen(false)}
          onBuildModeDraftChange={setBuildModeDraft}
          onAgentAccessModeDraftChange={setAgentAccessModeDraft}
          onModelDraftChange={setModelDraft}
          onNineRouterBaseUrlChange={setNineRouterBaseUrlDraft}
          onApiKeyChange={(p, k) => {
            if (p === "nine_router") setNineRouterApiKeyDraft(k);
          }}
          onTestRoute={testSelectedNineRouterRoute}
          onLogout={logoutToStart}
          onSave={saveSettings}
        />
      </Suspense>
      {renderNewProjectModal()}
      {renderProjectManagerModal()}

      <Topbar
        ws={ws}
        identity={identity}
        previewUrl={previewUrl}
        buildMode={buildMode}
        appTheme={appTheme}
        showExplorerPane={showExplorerPane}
        showAssistPane={showAssistPane}
        onQuickSwitchBuildMode={quickSwitchBuildMode}
        onToggleTheme={toggleAppTheme}
        onOpenSettings={openSettings}
        onOpenProjects={() => setProjectManagerOpen(true)}
        onDownloadProject={() => void downloadProjectToDevice()}
        onEnsurePreviewRunning={ensurePreviewRunning}
        onToggleExplorerPane={() => setShowExplorerPane((v) => !v)}
        onToggleAssistPane={() => setShowAssistPane((v) => !v)}
      />

      <div style={{ display: "none" }}>{hostedProjects.length}</div>
      <main className="appMain">
        <Suspense fallback={<ApporaLoading title="Loading workspace" subtitle="Bringing the IDE surface online." />}>
          {buildMode === "hybrid" ? renderHybridMode() : renderFullAgentMode()}
        </Suspense>
      </main>

      {renderFolderInput()}

      <AgentOrb
        ws={ws}
        buildMode={buildMode}
        agentStatus={agentStatus}
        agentReply={agentReply}
        agentWidgetOpen={agentWidgetOpen}
        agentOrbPosition={agentOrbPosition}
        workingMsg={workingMsg}
        agentLiveItems={agentLiveItems}
        agentRunViewPinned={agentRunViewPinned}
        editorStatus={editorStatus}
        activeFile={activeFile}
        previewUrl={previewUrl}
        agentInput={agentInput}
        attachedImage={attachedImage}
        imageUploading={imageUploading}
        onAgentInputChange={setAgentInput}
        onPickAgentImage={pickAgentImage}
        onClearAttachedImage={() => setAttachedImage(null)}
        onRunAgent={runAgentAndAutoApply}
        onEnsurePreviewRunning={ensurePreviewRunning}
        onToggleOpen={() => setAgentWidgetOpen(v => !v)}
        onResetRunView={resetAgentRunView}
        onSetPosition={setAgentOrbPosition}
      />
    </div>
  );
}
