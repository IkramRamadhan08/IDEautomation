import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent, type PointerEvent } from "react";
import type { Session } from "@supabase/supabase-js";
import { Toaster, toast } from "sonner";
import { supabase } from "./lib/supabase";

import "./app.css";
import {
  detectProjects,
  getIdentity,
  getModels,
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
  exportProjectZip,
  listCheckpoints,
  restoreCheckpoint,
  type HostedProject,
  type ProjectTemplate,
  type UserPreferences,
  type AgentCapabilities,
} from "./api";

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
  type AgentAuditSnapshot,
  type AgentLiveItem,
  type UploadedImageAsset,
} from "./types";

import { Topbar } from "./components/navigation/Topbar";
import { AgentOrb } from "./components/agent/AgentOrb";
import {
  ArrowRight,
  Bot,
  Boxes,
  Check,
  Code2,
  Database,
  Download,
  Globe2,
  HelpCircle,
  Layers3,
  Moon,
  Pencil,
  PlayCircle,
  RefreshCw,
  Rocket,
  ShieldCheck,
  Sparkles,
  Sun,
  Terminal,
  Trash2,
  Workflow,
  X,
} from "lucide-react";
import { runAgentWorkflow } from "./agent/workflow";
import { errorMessage, notifyToast } from "./app/feedback";
import { ensurePreviewRunningFlow, isHostedBrowser } from "./preview/runtime";

const SettingsModal = lazy(() => import("./components/settings/SettingsModal").then((module) => ({ default: module.SettingsModal })));
const HybridWorkspace = lazy(() => import("./modes/HybridWorkspace").then((module) => ({ default: module.HybridWorkspace })));
const FullAgentWorkspace = lazy(() => import("./modes/FullAgentWorkspace").then((module) => ({ default: module.FullAgentWorkspace })));
const astronautLoaderUrl = new URL("../Astronaut Illustration.webm", import.meta.url).href;

type AppTheme = "light" | "dark";

function ApporaLoading({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <div className="apporaLoadingShell" role="status" aria-live="polite">
      <div className="apporaLoadingOrbit" aria-hidden="true">
        <video
          className="apporaLoadingAstronaut"
          src={astronautLoaderUrl}
          autoPlay
          muted
          loop
          playsInline
        />
      </div>
      <div className="apporaLoadingCopy">
        <div className="apporaLoadingTitle">{title}</div>
        {subtitle ? <div className="apporaLoadingSubtitle">{subtitle}</div> : null}
      </div>
      <div className="apporaLoadingProgress" aria-hidden="true">
        <span />
      </div>
    </div>
  );
}

function getDefaultAssistPaneWidth() {
  if (typeof window === "undefined") return 280;
  return Math.max(220, Math.min(280, Math.floor(window.innerWidth * 0.24)));
}

export default function App() {
  const [ws, setWs] = useState<string | null>(null);
  const [identity, setIdentity] = useState<IdentityInfo | null>(null);
  const [googleAuth, setGoogleAuth] = useState<GoogleAuthStatus | null>(null);
  const [googleAuthLoading, setGoogleAuthLoading] = useState(true);

  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [hostedProjects, setHostedProjects] = useState<HostedProjectType[]>([]);
  const [selectedProject, setSelectedProject] = useState<string>(".");
  const [previewUrl, setPreviewUrl] = useState<string>("");
  const [previewFrameKey, setPreviewFrameKey] = useState(0);

  const [agentInput, setAgentInput] = useState<string>("");
  const [agentStatus, setAgentStatus] = useState<"idle" | "thinking" | "error">("idle");
  const [agentLog, setAgentLog] = useState<string>("");
  const [agentReply, setAgentReply] = useState<string>("");
  const [agentActions, setAgentActions] = useState<AgentAction[]>([]);
  const [agentWidgetOpen, setAgentWidgetOpen] = useState(false);
  const [attachedImage, setAttachedImage] = useState<UploadedImageAsset | null>(null);
  const [imageUploading, setImageUploading] = useState(false);

  const [agentOrbPosition, setAgentOrbPosition] = useState<{ x: number; y: number } | null>(null);
  const [workingMsg, setWorkingMsg] = useState<string>("");
  const [agentLiveItems, setAgentLiveItems] = useState<AgentLiveItem[]>([]);
  const [agentAuditTrail, setAgentAuditTrail] = useState<AgentAuditSnapshot[]>([]);
  const [agentCapabilities, setAgentCapabilities] = useState<AgentCapabilities | null>(null);

  const projectOptions = hostedProjects.length > 0
    ? hostedProjects.map((project) => ({ root: project.root, name: project.name }))
    : projects.map((project) => ({ root: project.root, name: project.name }));
  const [agentRunViewPinned, setAgentRunViewPinned] = useState(false);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [appTheme, setAppTheme] = useState<AppTheme>(() => {
    if (typeof window === "undefined") return "light";
    const saved = window.localStorage.getItem("appora-theme");
    return saved === "dark" || saved === "light" ? saved : "light";
  });
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [projectManagerOpen, setProjectManagerOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [projectTemplates, setProjectTemplates] = useState<ProjectTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState("saas-dashboard");
  const [newProjectSaving, setNewProjectSaving] = useState(false);
  const [projectsRefreshing, setProjectsRefreshing] = useState(false);
  const [projectMutatingId, setProjectMutatingId] = useState<string>("");
  const [projectExporting, setProjectExporting] = useState(false);
  const [projectEditingId, setProjectEditingId] = useState<string>("");
  const [projectRenameDraft, setProjectRenameDraft] = useState("");
  const [settings, setSettings] = useState<SettingsInfo | null>(null);
  const [llmProviderDraft, setLlmProviderDraft] = useState<ProviderChoice>("");
  const [buildMode, setBuildMode] = useState<BuildMode>("hybrid");
  const [buildModeDraft, setBuildModeDraft] = useState<BuildMode>("hybrid");
  const [modelDraft, setModelDraft] = useState<string>("");
  const [nineRouterBaseUrlDraft, setNineRouterBaseUrlDraft] = useState<string>("http://127.0.0.1:20128/v1");
  const [nineRouterApiKeyDraft, setNineRouterApiKeyDraft] = useState<string>("");
  const [openaiApiKeyDraft, setOpenaiApiKeyDraft] = useState<string>("");
  const [anthropicApiKeyDraft, setAnthropicApiKeyDraft] = useState<string>("");
  const [openrouterApiKeyDraft, setOpenrouterApiKeyDraft] = useState<string>("");
  const [groqApiKeyDraft, setGroqApiKeyDraft] = useState<string>("");
  const [geminiApiKeyDraft, setGeminiApiKeyDraft] = useState<string>("");
  const [togetherApiKeyDraft, setTogetherApiKeyDraft] = useState<string>("");
  const [cerebrasApiKeyDraft, setCerebrasApiKeyDraft] = useState<string>("");
  const [xaiApiKeyDraft, setXaiApiKeyDraft] = useState<string>("");
  const [models, setModels] = useState<string[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string>("");

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
  const [assistPaneWidth, setAssistPaneWidth] = useState(getDefaultAssistPaneWidth);

  const modelFromSettings = (provider: ProviderChoice, source: SettingsInfo | null): string => {
    if (!source) return "";
    if (provider === "nine_router") return source.nine_router_model || source.openrouter_model || "free-forever";
    if (provider === "openai") return source.openai_model || "";
    if (provider === "anthropic") return source.anthropic_model || "";
    if (provider === "openrouter") return source.openrouter_model || "";
    if (provider === "groq") return source.groq_model || "";
    if (provider === "gemini") return source.gemini_model || "";
    if (provider === "together") return source.together_model || "";
    if (provider === "cerebras") return source.cerebras_model || "";
    if (provider === "xai") return source.xai_model || "";
    return "";
  };

  useEffect(() => {
    document.documentElement.dataset.appTheme = appTheme;
    window.localStorage.setItem("appora-theme", appTheme);
  }, [appTheme]);

  const toggleAppTheme = () => setAppTheme((theme) => theme === "dark" ? "light" : "dark");

  const navigateTo = useCallback((path: string) => {
    if (typeof window === "undefined") return;
    const nextPath = path || "/";
    if (window.location.pathname !== nextPath) {
      window.history.pushState({}, "", nextPath);
    }
    setRoutePath(nextPath);
  }, []);

  useEffect(() => {
    const handlePopState = () => setRoutePath(window.location.pathname || "/");
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const modelFromPreferences = (provider: ProviderChoice, prefs: UserPreferences | null): string => {
    if (!prefs) return "";
    if (provider === "nine_router") return prefs.nine_router_model || prefs.openrouter_model || "free-forever";
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
  const [isResizingAssistPane, setIsResizingAssistPane] = useState(false);
  const [routePath, setRoutePath] = useState(() => {
    if (typeof window === "undefined") return "/";
    return window.location.pathname || "/";
  });

  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const agentLiveIdRef = useRef(0);
  const hasVerifiedHostedAuth = Boolean(settings?.supabase_enabled && googleAuth?.authenticated);
  const authUserKey = googleAuth?.authenticated
    ? googleAuth.user?.sub || googleAuth.user?.email || "authenticated"
    : "";
  const activeAuthUserRef = useRef<string | null>(null);
  const latestAuthUserKeyRef = useRef(authUserKey);
  latestAuthUserKeyRef.current = authUserKey;

  const makeAgentLiveId = useCallback(() => `agent-live-${Date.now()}-${agentLiveIdRef.current++}`,
    []);

  const pushAgentLiveItem = useCallback((item: Omit<AgentLiveItem, "id">) => {
    setAgentLiveItems((prev) => [...prev, { ...item, id: makeAgentLiveId() }]);
  }, [makeAgentLiveId]);

  const appendAssistantLiveText = useCallback((chunk: string, tone: AgentLiveItem["tone"] = "default") => {
    const clean = chunk.trim();
    if (!clean) return;
    setAgentLiveItems((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.role === "assistant" && last.tone === tone) {
        last.text = `${last.text}${last.text ? " " : ""}${clean}`;
        return next;
      }
      next.push({ id: makeAgentLiveId(), role: "assistant", tone, text: clean });
      return next;
    });
  }, [makeAgentLiveId]);

  const resetAgentRunView = useCallback(() => {
    setAgentRunViewPinned(false);
    setAgentLiveItems([]);
    setAgentAuditTrail([]);
  }, []);

  const clearWorkspaceUiState = useCallback(() => {
    setWs(null);
    setIdentity(null);
    setProjects([]);
    setHostedProjects([]);
    setSelectedProject(".");
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
    if (typeof window !== "undefined") {
      window.localStorage.removeItem("voiceide-demo-mode");
    }

    const applySessionAuth = (session: Session | null) => {
      if (session) {
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

    supabase.auth.getSession().then(({ data: { session } }) => {
      applySessionAuth(session);
      setGoogleAuthLoading(false);
    });

    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
      applySessionAuth(session);
    });

    return () => subscription.unsubscribe();
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

  useEffect(() => {
    const clampAssistWidth = () => {
      const minWidth = 220;
      const maxWidth = Math.min(300, Math.floor(window.innerWidth * 0.28));
      setAssistPaneWidth((prev) => Math.max(minWidth, Math.min(maxWidth, prev)));
    };

    clampAssistWidth();
    window.addEventListener("resize", clampAssistWidth);
    return () => window.removeEventListener("resize", clampAssistWidth);
  }, []);

  useEffect(() => {
    if (!isResizingAssistPane) return;

    const handleMouseMove = (event: MouseEvent) => {
      const minWidth = 220;
      const maxWidth = Math.min(300, Math.floor(window.innerWidth * 0.28));
      const nextWidth = window.innerWidth - event.clientX;
      setAssistPaneWidth(Math.max(minWidth, Math.min(maxWidth, nextWidth)));
    };

    const handleMouseUp = () => {
      setIsResizingAssistPane(false);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);

    return () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isResizingAssistPane]);

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
          nextProvider = (prefs.llm_provider || nextProvider) as ProviderChoice;
          nextModel = modelFromPreferences(nextProvider, prefs) || modelFromSettings(nextProvider, s);
        } catch {
          // ignore hosted preference load failures and keep global settings fallback
        }
      }

      setBuildMode(nextBuildMode);
      setBuildModeDraft(nextBuildMode);
      nextProvider = "nine_router";
      setLlmProviderDraft(nextProvider);
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
      setWs(info.path);
    } catch {
      // keep the current workspace state if a background restore check fails
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
    void startGoogleLogin("/app");
  };

  const logoutToStart = async () => {
    try {
      await supabase.auth.signOut();
    } catch {
      // ignore sign-out transport errors and clear local state anyway
    }
    resetClientIdentity();
    clearWorkspaceUiState();
    setGoogleAuth({ ok: true, authenticated: false, phase: "idle", user: null });
  };

  // --- Workspace & Files ---
  const refreshProjects = async () => {
    const requestAuthUserKey = latestAuthUserKeyRef.current;
    setProjectsRefreshing(true);
    try {
      const [detected, hosted, templates] = await Promise.all([
        detectProjects().catch(() => ({ ok: true, projects: [] as ProjectInfo[] })),
        hasVerifiedHostedAuth ? listHostedProjects().catch(() => ({ ok: true, projects: [] as HostedProject[] })) : Promise.resolve({ ok: true, projects: [] as HostedProject[] }),
        hasVerifiedHostedAuth ? listProjectTemplates().catch(() => ({ ok: true, templates: [] as ProjectTemplate[] })) : Promise.resolve({ ok: true, templates: [] as ProjectTemplate[] }),
      ]);
      if (requestAuthUserKey !== latestAuthUserKeyRef.current) return;
      setProjects(detected.projects || []);
      setHostedProjects(hosted.projects || []);
      setProjectTemplates(templates.templates || []);
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
      void refreshExplorer(".");
    }
  }, [ws]);

  useEffect(() => {
    if (hasVerifiedHostedAuth) {
      void refreshProjects();
    }
  }, [hasVerifiedHostedAuth]);

  useEffect(() => {
    if (selectedProject !== ".") return;
    if (hostedProjects.length > 0) {
      setSelectedProject(hostedProjects[0].root);
      return;
    }
    if (projects.length > 0) {
      setSelectedProject(projects[0].root);
    }
  }, [hostedProjects, projects, selectedProject]);

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
      if (!selectedProject || selectedProject === "." || !hasVerifiedHostedAuth) return;
      const hosted = hostedProjects.find((project) => project.root === selectedProject);
      if (!hosted) return;
      try {
        const prefRes = await getProjectPreferences(hosted.id);
        const prefs = prefRes.preferences;
        if (prefs.build_mode) {
          setBuildMode(prefs.build_mode);
          setBuildModeDraft(prefs.build_mode);
        }
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

  const openSavedProject = async (projectRoot: string) => {
    try {
      if (!ws) {
        const provisioned = await provisionWorkspace();
        setWs(provisioned.path);
      }
      selectProject(projectRoot);
      await refreshExplorer(projectRoot);
      setProjectManagerOpen(false);
      setEditorStatus(`Project ready: ${projectRoot}`);
      toast.success("Project dibuka");
    } catch (e) {
      toast.error("Gagal membuka project: " + errorMessage(e));
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
    try {
      const res = await renameHostedProject(project.id, { name });
      setHostedProjects((prev) => prev.map((item) => item.id === project.id ? res.project : item));
      setProjectEditingId("");
      setProjectRenameDraft("");
      toast.success("Project renamed");
    } catch (e) {
      toast.error("Gagal rename project: " + errorMessage(e));
    } finally {
      setProjectMutatingId("");
    }
  };

  const archiveProjectFromList = async (project: HostedProjectType) => {
    setProjectMutatingId(project.id);
    try {
      await archiveHostedProject(project.id);
      await refreshProjects();
      if (selectedProject === project.root) {
        const next = hostedProjects.find((item) => item.id !== project.id)?.root || ".";
        selectProject(next);
      }
      toast.success("Project archived");
    } catch (e) {
      toast.error("Gagal archive project: " + errorMessage(e));
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
    try {
      const exported = await exportProjectZip(target);
      const url = URL.createObjectURL(exported.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = exported.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      toast.success(`Project disimpan: ${exported.filename}`);
    } catch (e) {
      toast.error("Gagal download project: " + errorMessage(e));
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
    try {
      if (!ws) {
        const provisioned = await provisionWorkspace();
        setWs(provisioned.path);
      }

      const res = await createHostedProject({ name, template_id: selectedTemplateId || "blank" });
      if (hasVerifiedHostedAuth) {
        await updateProjectPreferences(res.project.id, { build_mode: buildModeDraft });
      }
      await refreshProjects();
      setSelectedProject(res.project.root);
      setEditorStatus(`Project ready: ${res.project.name}`);
      setNewProjectName("");
      setSelectedTemplateId("saas-dashboard");
      setNewProjectOpen(false);
      toast.success(`Project created: ${res.project.name}`);
    } catch (e) {
      toast.error("Gagal membuat project: " + errorMessage(e));
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

  const importAgentImage = async (fileList: FileList | null) => {
    const file = fileList?.[0];
    if (!file) return;
    setImageUploading(true);
    try {
      const uploaded = await uploadImageAsset(selectedProject, file);
      setAttachedImage(uploaded);
      toast.success(`Image attached: ${uploaded.name}`);
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

  const saveSettings = async () => {
    try {
      const patch: SettingsUpdate = {
        llm_provider: "nine_router",
        build_mode: buildModeDraft,
        nine_router_base_url: nineRouterBaseUrlDraft,
      };
      if (modelDraft) {
        patch.nine_router_model = modelDraft;
        if (llmProviderDraft === "openai") patch.openai_model = modelDraft;
        else if (llmProviderDraft === "anthropic") patch.anthropic_model = modelDraft;
        else if (llmProviderDraft === "openrouter") patch.openrouter_model = modelDraft;
        else if (llmProviderDraft === "groq") patch.groq_model = modelDraft;
        else if (llmProviderDraft === "gemini") patch.gemini_model = modelDraft;
        else if (llmProviderDraft === "together") patch.together_model = modelDraft;
        else if (llmProviderDraft === "cerebras") patch.cerebras_model = modelDraft;
        else if (llmProviderDraft === "xai") patch.xai_model = modelDraft;
      }
      if (nineRouterApiKeyDraft) patch.nine_router_api_key = nineRouterApiKeyDraft;
      if (openaiApiKeyDraft) patch.openai_api_key = openaiApiKeyDraft;
      if (anthropicApiKeyDraft) patch.anthropic_api_key = anthropicApiKeyDraft;
      if (openrouterApiKeyDraft) patch.openrouter_api_key = openrouterApiKeyDraft;
      if (groqApiKeyDraft) patch.groq_api_key = groqApiKeyDraft;
      if (geminiApiKeyDraft) patch.gemini_api_key = geminiApiKeyDraft;
      if (togetherApiKeyDraft) patch.together_api_key = togetherApiKeyDraft;
      if (cerebrasApiKeyDraft) patch.cerebras_api_key = cerebrasApiKeyDraft;
      if (xaiApiKeyDraft) patch.xai_api_key = xaiApiKeyDraft;

      await updateSettings(patch);

      if (hasVerifiedHostedAuth) {
        await updateUserPreferences({
          llm_provider: "nine_router",
          build_mode: buildModeDraft,
          openai_model: llmProviderDraft === "openai" ? modelDraft : null,
          anthropic_model: llmProviderDraft === "anthropic" ? modelDraft : null,
          nine_router_model: modelDraft,
          openrouter_model: llmProviderDraft === "openrouter" ? modelDraft : null,
          groq_model: llmProviderDraft === "groq" ? modelDraft : null,
          gemini_model: llmProviderDraft === "gemini" ? modelDraft : null,
          together_model: llmProviderDraft === "together" ? modelDraft : null,
          cerebras_model: llmProviderDraft === "cerebras" ? modelDraft : null,
          xai_model: llmProviderDraft === "xai" ? modelDraft : null,
        });
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
    await runAgentWorkflow({
      agentInput,
      agentStatus,
      buildMode,
      friendlyFreeTierMode: settings?.friendly_free_tier_mode ?? true,
      previewUrl,
      selectedProject,
      attachedImagePath: attachedImage?.path || null,
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
    void updateSettings({ build_mode: mode });
    if (hasVerifiedHostedAuth) {
      void updateUserPreferences({ build_mode: mode });
    }
  };

  // --- Renders ---
  const renderGoogleLoginGate = () => {
    const heroNodes = [
      { label: "Prompt", detail: "Describe the app", icon: Sparkles },
      { label: "Plan", detail: "Clara scopes the build", icon: Bot },
      { label: "Code", detail: "Raka edits files", icon: Code2 },
      { label: "Preview", detail: "Inspect in browser", icon: Globe2 },
      { label: "Memory", detail: "Project context stays", icon: Layers3 },
      { label: "Deploy", detail: "Vercel-ready output", icon: Rocket },
    ];
    const templateCards = [
      ["SaaS dashboard", "Auth, settings, billing-ready workspace"],
      ["AI landing page", "Conversion sections with provider routing"],
      ["Admin portal", "Tables, forms, permissions, activity"],
      ["Portfolio app", "Content pages, templates, deployment"],
    ];
    const providers = ["OpenAI", "Gemini", "Anthropic", "OpenRouter", "Groq", "Together", "Cerebras", "xAI", "Supabase", "Vercel"];
    const triggerLandingBurst = (target: HTMLDivElement) => {
      target.classList.remove("apporaBursting");
      target.style.setProperty("--appora-burst", "0");
      void target.offsetWidth;
      target.classList.add("apporaBursting");
      target.style.setProperty("--appora-burst", "1");
      window.setTimeout(() => {
        target.classList.remove("apporaBursting");
        target.style.setProperty("--appora-burst", "0");
      }, 680);
    };
    const handleLandingPointerMove = (event: PointerEvent<HTMLDivElement>) => {
      const target = event.currentTarget;
      target.style.setProperty("--appora-cursor-x", `${event.clientX}px`);
      target.style.setProperty("--appora-cursor-y", `${event.clientY}px`);
      target.style.setProperty("--appora-cursor-hot", "1");
    };
    const handleLandingPointerLeave = (event: PointerEvent<HTMLDivElement>) => {
      event.currentTarget.style.setProperty("--appora-cursor-hot", "0");
    };

    return (
    <div
      className="authLanding apporaLanding"
      id="top"
      onPointerMove={handleLandingPointerMove}
      onPointerLeave={handleLandingPointerLeave}
      onPointerDownCapture={(event) => triggerLandingBurst(event.currentTarget)}
    >
      <Toaster position="top-right" richColors />
      <div className="apporaCursorLiquid" aria-hidden="true">
        <span className="apporaCursorCore" />
        <span className="apporaCursorBlob one" />
        <span className="apporaCursorBlob two" />
        <span className="apporaCursorBlob three" />
        <span className="apporaCursorDrop d1" />
        <span className="apporaCursorDrop d2" />
        <span className="apporaCursorDrop d3" />
        <span className="apporaCursorDrop d4" />
        <span className="apporaCursorDrop d5" />
        <span className="apporaCursorDrop d6" />
        <span className="apporaCursorDrop d7" />
        <span className="apporaCursorDrop d8" />
        <span className="apporaCursorDrop d9" />
        <span className="apporaCursorGlint" />
      </div>
      <header className="apporaNav">
        <a className="splineBrandButton apporaBrand" href="#top" aria-label="Appora home">
          <span className="authBrandMark">A</span>
          <span>Appora</span>
        </a>
        <nav className="apporaNavLinks" aria-label="Landing navigation">
          <a href="#templates">Templates</a>
          <a href="#tutorial">Tutorial</a>
          <a href="#docs">Docs</a>
          <a href="#faq">FAQ</a>
        </nav>
        <div className="apporaNavActions">
          <button className="apporaThemeToggle" type="button" onClick={toggleAppTheme} title={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`} aria-label={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`}>
            {appTheme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>
          <button className="apporaNavCta" onClick={openAppora}>
            Start Building
            <ArrowRight size={16} />
          </button>
        </div>
      </header>

      <main className="apporaMain">
        <section className="apporaHero" aria-label="Appora">
          <div className="apporaHeroCopy">
            <h1>THE FUTURE JUST IN YOUR HEAD</h1>
            <p>
              Appora turns a rough idea into a hosted workspace with Clara for planning, Raka for implementation,
              live preview, project memory, Supabase data, and Vercel-ready output.
            </p>
            <div className="apporaHeroActions">
              <button className="apporaPrimaryButton" onClick={openAppora}>
                Continue With Google
                <ArrowRight size={17} />
              </button>
              <a className="apporaSecondaryButton" href="#tutorial">
                Watch The Flow
                <PlayCircle size={17} />
              </a>
            </div>
            <div className="apporaSignalRow" aria-label="Platform signals">
              <span>BYOK Models</span>
              <span>Serverless Ready</span>
              <span>Project Memory</span>
            </div>
          </div>

          <div className="apporaHeroVisual" aria-label="Agent workflow preview">
            <div className="apporaOrbitGlow" />
            <div className="apporaMascotStage" aria-label="Clara and Raka companions">
              {[
                { name: "Clara", role: "Planner", tone: "clara" },
                { name: "Raka", role: "Builder", tone: "raka" },
              ].map((agent) => (
                <div className={`apporaMascot ${agent.tone}`} key={agent.name}>
                  <div className="apporaMascotHalo" />
                  <div className="apporaMascotBody">
                    <div className="apporaMascotHelmet">
                      <span className="apporaMascotAntenna" />
                      <div className="apporaMascotFace">
                        <span className="apporaMascotEye left" />
                        <span className="apporaMascotEye right" />
                        <span className="apporaMascotMouth" />
                      </div>
                    </div>
                    <div className="apporaMascotSuit">
                      <span />
                      <strong>{agent.name.slice(0, 1)}</strong>
                    </div>
                  </div>
                  <div className="apporaMascotTag">
                    <strong>{agent.name}</strong>
                    <span>{agent.role}</span>
                  </div>
                </div>
              ))}
            </div>
            <div className="apporaCommandPanel">
              <div className="apporaPanelChrome">
                <span />
                <span />
                <span />
                <strong>appora://workspace</strong>
              </div>
              <div className="apporaPromptLine">Build a booking app with auth, admin dashboard, and deploy notes.</div>
              <div className="apporaAgentRows">
                <div><Bot size={17} /><strong>Clara</strong><span>breaks the request into product, data, UI, and test steps</span></div>
                <div><Terminal size={17} /><strong>Raka</strong><span>edits files, runs checks, and records actions only</span></div>
              </div>
            </div>
            <div className="apporaNodeGrid">
              {heroNodes.map(({ label, detail, icon: Icon }) => (
                <div className="apporaNode" key={label}>
                  <Icon size={18} />
                  <strong>{label}</strong>
                  <span>{detail}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="apporaBand apporaMarqueeBand" aria-label="Supported providers">
          <div className="apporaMarquee">
            {[...providers, ...providers].map((provider, index) => (
              <span key={`${provider}-${index}`}>{provider}</span>
            ))}
          </div>
        </section>

        <section className="apporaSection apporaSplit" id="tutorial">
          <div className="apporaSectionCopy">
            <span className="apporaSectionIndex">01</span>
            <h2>From Chat To Working Project Without Switching Tools.</h2>
            <p>Start with normal language. The agent plans the project, edits files, runs terminal actions, previews the result, and keeps every action visible.</p>
          </div>
          <div className="apporaWorkflowStack">
            {[
              ["Ask", "Describe the product, audience, pages, and integrations."],
              ["Build", "Agent writes code with patch-based edits and command history."],
              ["Inspect", "Preview and browser checks catch broken UI before deploy."],
              ["Ship", "Hosted project state stays connected to Supabase and Vercel."],
            ].map(([title, copy], index) => (
              <article key={title}>
                <em>{String(index + 1).padStart(2, "0")}</em>
                <strong>{title}</strong>
                <span>{copy}</span>
              </article>
            ))}
          </div>
        </section>

        <section className="apporaSection apporaAgents" aria-label="Agents">
          <div className="apporaSectionHeader">
            <span className="apporaSectionIndex">02</span>
            <h2>Two Agents, One Build Loop.</h2>
          </div>
          <div className="apporaAgentCards">
            <article>
              <div className="apporaAgentAvatar clara">C</div>
              <h3>Clara</h3>
              <p>Conversational planner. She keeps the response stream human, asks for missing context, and turns vague requests into clear implementation intent.</p>
            </article>
            <article>
              <div className="apporaAgentAvatar raka">R</div>
              <h3>Raka</h3>
              <p>Execution agent. He focuses on edits, terminal commands, tool calls, verification, and the action-only interaction log.</p>
            </article>
          </div>
        </section>

        <section className="apporaSection" id="templates">
          <div className="apporaSectionHeader wide">
            <span className="apporaSectionIndex">03</span>
            <h2>Production-Shaped Starters For People Who Do Not Want A Blank Repo.</h2>
          </div>
          <div className="apporaTemplateGrid">
            {templateCards.map(([title, copy]) => (
              <article key={title}>
                <Boxes size={19} />
                <strong>{title}</strong>
                <span>{copy}</span>
              </article>
            ))}
          </div>
        </section>

        <section className="apporaSection apporaSplit" id="docs">
          <div className="apporaSectionCopy">
            <span className="apporaSectionIndex">04</span>
            <h2>Bring Your Own Keys, Use The Models People Actually Have.</h2>
            <p>Provider routing is built for free-tier friendly experimentation: OpenAI, Gemini, OpenRouter, Groq, Together, Cerebras, xAI, and more through hosted settings.</p>
          </div>
          <div className="apporaDocsPanel">
            <div><ShieldCheck size={18} /><strong>Hosted settings</strong><span>API keys stay in user-scoped provider secrets.</span></div>
            <div><Database size={18} /><strong>Supabase memory</strong><span>Project files, settings, job ledger, and memory chunks persist.</span></div>
            <div><Workflow size={18} /><strong>MCP/tool loop</strong><span>Actions are separated from assistant responses for clearer UX.</span></div>
          </div>
        </section>

        <section className="apporaSection apporaFaqSection" id="faq">
          <div className="apporaSectionHeader">
            <span className="apporaSectionIndex">05</span>
            <h2>Built For Hosted Web, Not A Local-Only Toy.</h2>
          </div>
          <div className="apporaFaqGrid">
            {[
              ["Can It Deploy?", "The app is shaped for Vercel serverless with Supabase as the durable backend."],
              ["Can Beginners Use It?", "The primary flow is prompt, preview, edit with agent help, then ship from a saved project."],
              ["Can I Use Free Models?", "Yes. The provider layer supports BYOK model routing so users can pick what they already have."],
              ["Where Do Actions Show?", "Agent actions belong in Live Interaction. Model conversation streams through the orb/chat surface."],
            ].map(([question, answer]) => (
              <article key={question}>
                <HelpCircle size={18} />
                <strong>{question}</strong>
                <span>{answer}</span>
              </article>
            ))}
          </div>
        </section>

        <section className="apporaFinalCta">
          <h2>Start With An Idea. Leave With A Project You Can Keep Improving.</h2>
          <button className="apporaPrimaryButton" onClick={openAppora}>
            Open Appora
            <ArrowRight size={17} />
          </button>
        </section>
      </main>

      <footer className="apporaFooter">
        <div className="apporaFooterShell">
          <div className="apporaFooterBrand">
            <span className="authBrandMark">A</span>
            <div>
              <strong>Appora</strong>
              <span>Agentic web builder for hosted projects.</span>
            </div>
          </div>
          <div className="apporaFooterColumns">
            <nav aria-label="Footer product navigation">
              <strong>Product</strong>
              <a href="#templates">Templates</a>
              <a href="#tutorial">Tutorial</a>
            </nav>
            <nav aria-label="Footer platform navigation">
              <strong>Platform</strong>
              <a href="#docs">Docs</a>
              <a href="#faq">FAQ</a>
            </nav>
            <div className="apporaFooterSignal">
              <strong>Ready for</strong>
              <span>Supabase memory</span>
              <span>Vercel deploys</span>
            </div>
          </div>
          <div className="apporaFooterBottom">
            <span>Bring your own keys. Keep your projects saved.</span>
            <button className="apporaFooterCta" type="button" onClick={openAppora}>
              Open Appora
              <ArrowRight size={15} />
            </button>
          </div>
        </div>
      </footer>
    </div>
  );
  };

  const renderNewProjectModal = () => (
    newProjectOpen ? (
      <div className="modalBackdrop" onClick={() => !newProjectSaving && setNewProjectOpen(false)}>
        <form className="newProjectModal pane" onSubmit={createHostedProjectFromName} onClick={(event) => event.stopPropagation()}>
          <div className="newProjectModalHeader">
            <div>
              <div className="savedProjectsEyebrow">New project</div>
              <div className="newProjectModalTitle">Bikin project baru</div>
            </div>
            <button className="btn subtleBtn" type="button" disabled={newProjectSaving} onClick={() => setNewProjectOpen(false)}>
              Close
            </button>
          </div>
          <label className="newProjectField">
            <span>Project name</span>
            <input
              className="input"
              value={newProjectName}
              onChange={(event) => setNewProjectName(event.target.value)}
              placeholder="Contoh: Portfolio Raka"
              disabled={newProjectSaving}
              autoFocus
            />
          </label>
          <div className="newProjectTemplateSection">
            <div className="newProjectTemplateHeader">
              <span>Starter template</span>
              <small>{projectTemplates.length > 0 ? "Production-ready starting point" : "Loading templates..."}</small>
            </div>
            <div className="newProjectTemplateGrid">
              {(projectTemplates.length > 0 ? projectTemplates : [
                { id: "saas-dashboard", name: "SaaS Dashboard", category: "Dashboard", description: "Auth-ready dashboard starter.", best_for: "SaaS MVPs and portals.", tags: ["dashboard"] },
              ]).map((template) => (
                <button
                  key={template.id}
                  className={`templateChoice ${selectedTemplateId === template.id ? "selected" : ""}`}
                  type="button"
                  disabled={newProjectSaving}
                  onClick={() => setSelectedTemplateId(template.id)}
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
            <button className="btn" type="button" disabled={newProjectSaving} onClick={() => setNewProjectOpen(false)}>Cancel</button>
            <button className="btn primary" type="submit" disabled={!newProjectName.trim() || newProjectSaving}>
              {newProjectSaving ? "Creating..." : "Create project"}
            </button>
          </div>
        </form>
      </div>
    ) : null
  );

  const renderSavedProjectsPanel = (variant: "setup" | "modal" = "setup") => (
    <div className={`savedProjectsPanel ${variant === "modal" ? "projectManagerPanel" : ""}`}>
      <div className="savedProjectsHeader">
        <div>
          <div className="savedProjectsEyebrow">Saved projects</div>
          <div className="savedProjectsTitle">Project yang pernah dibuat</div>
        </div>
        <div className="savedProjectsHeaderActions">
          <button className="btn subtleBtn" type="button" onClick={() => void refreshProjects()} disabled={projectsRefreshing}>
            <RefreshCw size={14} className={projectsRefreshing ? "spinIcon" : ""} />
            <span>{projectsRefreshing ? "Refreshing" : "Refresh"}</span>
          </button>
          <button className="btn subtleBtn" type="button" disabled={projectExporting || !selectedProject || selectedProject === "."} onClick={() => void downloadProjectToDevice()}>
            <Download size={14} className={projectExporting ? "spinIcon" : ""} />
            <span>{projectExporting ? "Saving" : "Save ZIP"}</span>
          </button>
          <button className="btn primary" type="button" onClick={() => setNewProjectOpen(true)}>
            New project
          </button>
        </div>
      </div>
      {hostedProjects.length > 0 ? (
        <div className="savedProjectsList">
          {hostedProjects.map((project) => {
            const editing = projectEditingId === project.id;
            const busy = projectMutatingId === project.id;
            return (
              <div key={project.id} className={`savedProjectItem ${selectedProject === project.root ? "active" : ""}`}>
                <span className="savedProjectMain">
                  {editing ? (
                    <input
                      className="savedProjectRenameInput"
                      value={projectRenameDraft}
                      onChange={(event) => setProjectRenameDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void saveProjectRename(project);
                        if (event.key === "Escape") {
                          setProjectEditingId("");
                          setProjectRenameDraft("");
                        }
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
                      <button className="iconOnlyBtn" type="button" disabled={busy} onClick={() => void saveProjectRename(project)} title="Save rename">
                        <Check size={14} />
                      </button>
                      <button className="iconOnlyBtn" type="button" disabled={busy} onClick={() => { setProjectEditingId(""); setProjectRenameDraft(""); }} title="Cancel rename">
                        <X size={14} />
                      </button>
                    </>
                  ) : (
                    <>
                      <button className="savedProjectOpen" type="button" disabled={busy} onClick={() => void openSavedProject(project.root)}>
                        Open
                      </button>
                      <button className="iconOnlyBtn" type="button" disabled={projectExporting} onClick={() => void downloadProjectToDevice(project.root)} title="Download ZIP">
                        <Download size={14} />
                      </button>
                      <button className="iconOnlyBtn" type="button" disabled={busy} onClick={() => beginRenameProject(project)} title="Rename project">
                        <Pencil size={14} />
                      </button>
                      <button className="iconOnlyBtn danger" type="button" disabled={busy} onClick={() => void archiveProjectFromList(project)} title="Archive project">
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

  const renderProjectManagerModal = () => (
    projectManagerOpen ? (
      <div className="modalBackdrop" onClick={() => setProjectManagerOpen(false)}>
        <div className="newProjectModal projectManagerModal pane" onClick={(event) => event.stopPropagation()}>
          <div className="newProjectModalHeader">
            <div>
              <div className="savedProjectsEyebrow">Project manager</div>
              <div className="newProjectModalTitle">Kelola project</div>
            </div>
            <button className="btn subtleBtn" type="button" onClick={() => setProjectManagerOpen(false)}>
              Close
            </button>
          </div>
          {renderSavedProjectsPanel("modal")}
        </div>
      </div>
    ) : null
  );

  const renderWorkspaceOnboarding = () => (
    <div className="workspaceGateWrap workspaceSetupWrap" id="top">
      {renderNewProjectModal()}
      <header className="apporaNav workspaceSetupNav">
        <a className="splineBrandButton apporaBrand" href="#top" aria-label="Appora home">
          <span className="authBrandMark">A</span>
          <span>Appora</span>
        </a>
        <div className="workspaceSetupNavMeta">
          <span>{identity?.display_name || identity?.email || "Signed in"}</span>
        </div>
        <div className="apporaNavActions">
          <button className="apporaThemeToggle" type="button" onClick={toggleAppTheme} title={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`} aria-label={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`}>
            {appTheme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>
          <button className="apporaNavCta" type="button" onClick={logoutToStart}>
            Logout
          </button>
        </div>
      </header>
      <main className="workspaceSetupMain">
        <div className="workspaceGateCard pane workspaceSetupCard">
          <div className="workspaceGateKicker">Project setup</div>
          <div className="workspaceGateTitle">Start a project for this session</div>
          <div className="workspaceGateSubtitle">
            Open an existing project, upload one, or create a new Supabase-backed project for the agent.
            {isHostedBrowser() ? " Project text files are restored from Supabase between serverless runs." : ""}
          </div>
          <div className="workspaceGateActions">
            <button className="btn primary" onClick={pickWorkspace}>{isHostedBrowser() ? "Upload project…" : "Open project…"}</button>
            <button className="btn" onClick={() => setNewProjectOpen(true)}>New project</button>
          </div>
          <div className="workspaceGateFeatureGrid">
            <div className="gateFeatureCard">
              <div className="gateFeatureTitle">Open or upload project</div>
              <div className="gateFeatureText">Best when you already have a repo and want to keep working immediately.</div>
            </div>
            <div className="gateFeatureCard">
              <div className="gateFeatureTitle">Create new project</div>
              <div className="gateFeatureText">Best when you want Clara or Raka to scaffold a fresh app from a simple brief.</div>
            </div>
          </div>
          {hasVerifiedHostedAuth ? renderSavedProjectsPanel("setup") : null}
          {renderFolderInput()}
        </div>
      </main>
    </div>
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
  if (!ws || (hasVerifiedHostedAuth && hostedProjects.length === 0 && projects.length === 0 && selectedProject === ".")) return renderWorkspaceOnboarding();

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
      onStartResizeAssistPane={() => setIsResizingAssistPane(true)}
      onEnsurePreviewRunning={ensurePreviewRunning}
      appTheme={appTheme}
    />
  );

  const renderFullAgentMode = () => (
    <FullAgentWorkspace
      ws={ws}
      selectedProject={selectedProject}
      previewUrl={previewUrl}
      previewFrameKey={previewFrameKey}
      agentStatus={agentStatus}
      workingMsg={workingMsg}
      agentLog={agentLog}
      agentActions={agentActions}
      agentLiveItems={agentLiveItems}
      agentAuditTrail={agentAuditTrail}
      attachedAssetName={attachedImage?.name || null}
      onEnsurePreviewRunning={ensurePreviewRunning}
    />
  );

  return (
    <div className={`shell appTheme-${appTheme}`}>
      <Toaster position="top-right" richColors />
      {hostedProjects.length > 0 ? null : null}
      <input ref={folderInputRef} type="file" multiple style={{ display: "none" }} onChange={e => importPickedFolder(e.target.files)} />
      <input ref={imageInputRef} type="file" accept="image/*" style={{ display: "none" }} onChange={e => importAgentImage(e.target.files)} />
      
      <Suspense fallback={null}>
        <SettingsModal
          settingsOpen={settingsOpen}
          identity={identity}
          settings={settings}
          llmProviderDraft={llmProviderDraft}
          buildModeDraft={buildModeDraft}
          modelDraft={modelDraft}
          nineRouterBaseUrlDraft={nineRouterBaseUrlDraft}
          nineRouterApiKeyDraft={nineRouterApiKeyDraft}
          openaiApiKeyDraft={openaiApiKeyDraft}
          anthropicApiKeyDraft={anthropicApiKeyDraft}
          openrouterApiKeyDraft={openrouterApiKeyDraft}
          groqApiKeyDraft={groqApiKeyDraft}
          geminiApiKeyDraft={geminiApiKeyDraft}
          togetherApiKeyDraft={togetherApiKeyDraft}
          cerebrasApiKeyDraft={cerebrasApiKeyDraft}
          xaiApiKeyDraft={xaiApiKeyDraft}
          models={models}
          modelsLoading={modelsLoading}
          modelsError={modelsError}
          onClose={() => setSettingsOpen(false)}
          onLlmProviderChange={p => {
            const provider = p || "nine_router";
            setLlmProviderDraft(provider);
            setModelDraft(modelFromSettings(provider, settings));
            void loadProviderModels(provider);
          }}
          onBuildModeDraftChange={setBuildModeDraft}
          onModelDraftChange={setModelDraft}
          onNineRouterBaseUrlChange={setNineRouterBaseUrlDraft}
          onApiKeyChange={(p, k) => {
            if (p === "nine_router") setNineRouterApiKeyDraft(k);
            else
            if (p === "openai") setOpenaiApiKeyDraft(k);
            else if (p === "anthropic") setAnthropicApiKeyDraft(k);
            else if (p === "openrouter") setOpenrouterApiKeyDraft(k);
            else if (p === "groq") setGroqApiKeyDraft(k);
            else if (p === "gemini") setGeminiApiKeyDraft(k);
            else if (p === "together") setTogetherApiKeyDraft(k);
            else if (p === "cerebras") setCerebrasApiKeyDraft(k);
            else if (p === "xai") setXaiApiKeyDraft(k);
          }}
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
