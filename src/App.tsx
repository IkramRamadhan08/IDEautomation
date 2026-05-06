import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
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
  importBrowserFolder,
  uploadImageAsset,
  listHostedProjects,
  createHostedProject,
  type HostedProject,
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
import { runAgentWorkflow } from "./agent/workflow";
import { errorMessage, notifyToast } from "./app/feedback";
import { ensurePreviewRunningFlow, isHostedBrowser } from "./preview/runtime";

const SettingsModal = lazy(() => import("./components/settings/SettingsModal").then((module) => ({ default: module.SettingsModal })));
const HybridWorkspace = lazy(() => import("./modes/HybridWorkspace").then((module) => ({ default: module.HybridWorkspace })));
const FullAgentWorkspace = lazy(() => import("./modes/FullAgentWorkspace").then((module) => ({ default: module.FullAgentWorkspace })));

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
  const [agentRunViewPinned, setAgentRunViewPinned] = useState(false);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<SettingsInfo | null>(null);
  const [llmProviderDraft, setLlmProviderDraft] = useState<ProviderChoice>("");
  const [buildMode, setBuildMode] = useState<BuildMode>("hybrid");
  const [buildModeDraft, setBuildModeDraft] = useState<BuildMode>("hybrid");
  const [modelDraft, setModelDraft] = useState<string>("");
  const [openaiApiKeyDraft, setOpenaiApiKeyDraft] = useState<string>("");
  const [anthropicApiKeyDraft, setAnthropicApiKeyDraft] = useState<string>("");
  const [openrouterApiKeyDraft, setOpenrouterApiKeyDraft] = useState<string>("");
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
  const [isResizingAssistPane, setIsResizingAssistPane] = useState(false);

  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const agentLiveIdRef = useRef(0);
  const hasVerifiedHostedAuth = Boolean(settings?.supabase_enabled && googleAuth?.authenticated);

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
    setWs(null);
  }, [googleAuth?.authenticated, googleAuth?.phase]);

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
    try {
      const s = await getSettings();
      setSettings(s);

      let nextBuildMode = s.build_mode || "hybrid";
      let nextProvider = (s.llm_provider || "") as ProviderChoice;
      let nextModel = "";

      if (s.supabase_enabled && googleAuth?.authenticated) {
        try {
          const prefRes = await getUserPreferences();
          const prefs = prefRes.preferences;
          nextBuildMode = prefs.build_mode || nextBuildMode;
          nextProvider = (prefs.llm_provider || nextProvider) as ProviderChoice;
          if (nextProvider === "openai") nextModel = prefs.openai_model || s.openai_model || "";
          else if (nextProvider === "anthropic") nextModel = prefs.anthropic_model || s.anthropic_model || "";
          else if (nextProvider === "openrouter") nextModel = prefs.openrouter_model || s.openrouter_model || "";
        } catch {
          // ignore hosted preference load failures and keep global settings fallback
        }
      }

      setBuildMode(nextBuildMode);
      setBuildModeDraft(nextBuildMode);
      setLlmProviderDraft(nextProvider);
      setModelDraft(nextModel || (nextProvider === "openai" ? s.openai_model : nextProvider === "anthropic" ? s.anthropic_model : nextProvider === "openrouter" ? s.openrouter_model : ""));
    } catch { /* ignore */ }
  };

  const loadIdentityOverview = async () => {
    try {
      const info = await getIdentity();
      setIdentity(info);
    } catch { /* ignore */ }
  };

  const loadWorkspaceOverview = async () => {
    try {
      const info = await getWorkspace();
      setWs(info.path);
    } catch {
      // keep the current workspace state if a background restore check fails
    }
  };

  const startGoogleLogin = async () => {
    try {
      const { error } = await supabase.auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: window.location.origin },
      });
      if (error) throw error;
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const logoutToStart = async () => {
    try {
      await supabase.auth.signOut();
    } catch {
      // ignore sign-out transport errors and clear local state anyway
    }
    resetClientIdentity();
    setWs(null);
    setIdentity(null);
    setGoogleAuth({ ok: true, authenticated: false, phase: "idle", user: null });
  };

  // --- Workspace & Files ---
  const refreshProjects = async () => {
    try {
      const [detected, hosted] = await Promise.all([
        detectProjects().catch(() => ({ ok: true, projects: [] as ProjectInfo[] })),
        hasVerifiedHostedAuth ? listHostedProjects().catch(() => ({ ok: true, projects: [] as HostedProject[] })) : Promise.resolve({ ok: true, projects: [] as HostedProject[] }),
      ]);
      setProjects(detected.projects || []);
      setHostedProjects(hosted.projects || []);
    } catch {
      setProjects([]);
      setHostedProjects([]);
    }
  };

  const refreshExplorer = async (path = ".") => {
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
      if (path === ".") setExplorerItems(items);
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

  const createManagedWorkspace = async () => {
    try {
      const res = await provisionWorkspace();
      setWs(res.path);
      setEditorStatus(`Workspace ready: ${res.path}`);
      await refreshProjects();
      toast.success("Workspace siap");
    } catch (e) {
      setEditorStatus("Failed to create workspace");
      toast.error("Gagal membuat workspace: " + errorMessage(e));
    }
  };

  const createHostedProjectFromPrompt = async () => {
    const name = window.prompt("Project name:");
    if (!name?.trim()) return;
    try {
      if (!ws) {
        const provisioned = await provisionWorkspace();
        setWs(provisioned.path);
      }

      const res = await createHostedProject({ name: name.trim() });
      if (hasVerifiedHostedAuth) {
        await updateProjectPreferences(res.project.id, { build_mode: buildModeDraft });
      }
      await refreshProjects();
      setSelectedProject(res.project.root);
      setEditorStatus(`Project ready: ${res.project.name}`);
      toast.success(`Project created: ${res.project.name}`);
    } catch (e) {
      toast.error("Gagal membuat project: " + errorMessage(e));
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
    if (llmProviderDraft) await loadProviderModels(llmProviderDraft);
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
        const fallback = p === "openai"
          ? settings?.openai_model
          : p === "anthropic"
            ? settings?.anthropic_model
            : p === "openrouter"
              ? settings?.openrouter_model
              : "";
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
        llm_provider: llmProviderDraft,
        build_mode: buildModeDraft,
      };
      if (modelDraft) {
        if (llmProviderDraft === "openai") patch.openai_model = modelDraft;
        else if (llmProviderDraft === "anthropic") patch.anthropic_model = modelDraft;
        else if (llmProviderDraft === "openrouter") patch.openrouter_model = modelDraft;
      }
      if (openaiApiKeyDraft) patch.openai_api_key = openaiApiKeyDraft;
      if (anthropicApiKeyDraft) patch.anthropic_api_key = anthropicApiKeyDraft;
      if (openrouterApiKeyDraft) patch.openrouter_api_key = openrouterApiKeyDraft;

      await updateSettings(patch);

      if (hasVerifiedHostedAuth) {
        await updateUserPreferences({
          llm_provider: llmProviderDraft || null,
          build_mode: buildModeDraft,
          openai_model: llmProviderDraft === "openai" ? modelDraft : null,
          anthropic_model: llmProviderDraft === "anthropic" ? modelDraft : null,
          openrouter_model: llmProviderDraft === "openrouter" ? modelDraft : null,
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
  const renderGoogleLoginGate = () => (
    <div className="workspaceGateWrap authGateWrap">
      <div className="workspaceGateCard pane authGateCard">
        <div className="workspaceGateKicker">Voice IDE</div>
        <div className="workspaceGateTitle">A calmer way to build production UI</div>
        <div className="workspaceGateSubtitle">
          Edit code directly, run live preview instantly, and pull in agent help only when you want precise changes.
        </div>
        <div className="workspaceGateFeatureGrid">
          <div className="gateFeatureCard">
            <div className="gateFeatureTitle">Manual control first</div>
            <div className="gateFeatureText">Keep the file tree, editor, and preview visible while you iterate.</div>
          </div>
          <div className="gateFeatureCard">
            <div className="gateFeatureTitle">Agent when useful</div>
            <div className="gateFeatureText">Ask Clara for scoped UI polish, refactors, and implementation help.</div>
          </div>
          <div className="gateFeatureCard">
            <div className="gateFeatureTitle">Ready for deploy</div>
            <div className="gateFeatureText">Shape the app with a workflow that maps cleanly to Vercel and Railway.</div>
          </div>
        </div>
        <div className="workspaceGateActions">
          <button className="btn primary" onClick={startGoogleLogin}>Continue with Google</button>
        </div>
      </div>
    </div>
  );

  const renderWorkspaceOnboarding = () => (
    <div className="workspaceGateWrap">
      <div className="workspaceGateCard pane">
        <div className="workspaceGateKicker">Workspace setup</div>
        <div className="workspaceGateTitle">Choose where this session should build</div>
        <div className="workspaceGateSubtitle">
          Open an existing hosted project, or create a new Supabase-backed workspace for the agent.
          {isHostedBrowser() ? " Project text files are restored from Supabase between serverless runs." : ""}
        </div>
        <div className="workspaceGateFeatureGrid">
          <div className="gateFeatureCard">
            <div className="gateFeatureTitle">Open existing project</div>
            <div className="gateFeatureText">Best when you already have a repo and want to keep working immediately.</div>
          </div>
          <div className="gateFeatureCard">
            <div className="gateFeatureTitle">Create new workspace</div>
            <div className="gateFeatureText">Best when you want an isolated place to scaffold and ship without clutter.</div>
          </div>
        </div>
        <div className="workspaceGateActions">
          <button className="btn primary" onClick={pickWorkspace}>{isHostedBrowser() ? "Upload project…" : "Open project…"}</button>
          <button className="btn" onClick={createManagedWorkspace}>New workspace</button>
          <button className="btn" onClick={createHostedProjectFromPrompt}>New project</button>
          <button className="btn" onClick={logoutToStart}>Logout</button>
        </div>
        {hasVerifiedHostedAuth && hostedProjects.length > 0 ? (
          <div className="settingsSubtle" style={{ marginTop: 12 }}>
            Existing projects: {hostedProjects.map((project) => project.name).join(", ")}
          </div>
        ) : null}
        {renderFolderInput()}
      </div>
    </div>
  );

  if (googleAuthLoading) {
    return (
      <div className="workspaceGateWrap">
        <div className="workspaceGateCard pane">
          <div className="workspaceGateTitle">Loading…</div>
        </div>
      </div>
    );
  }

  if (!googleAuth?.authenticated) return renderGoogleLoginGate();
  if (!ws) return renderWorkspaceOnboarding();

  const renderHybridMode = () => (
    <HybridWorkspace
      ws={ws}
      selectedProject={selectedProject}
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
      attachedAssetName={attachedImage?.name || null}
      recentActions={agentActions}
      agentLiveItems={agentLiveItems}
      agentAuditTrail={agentAuditTrail}
      onRefreshExplorer={refreshExplorer}
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
      agentReply={agentReply}
      agentLog={agentLog}
      agentActions={agentActions}
      agentLiveItems={agentLiveItems}
      agentAuditTrail={agentAuditTrail}
      attachedAssetName={attachedImage?.name || null}
      onEnsurePreviewRunning={ensurePreviewRunning}
    />
  );

  return (
    <div className="shell">
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
          openaiApiKeyDraft={openaiApiKeyDraft}
          anthropicApiKeyDraft={anthropicApiKeyDraft}
          openrouterApiKeyDraft={openrouterApiKeyDraft}
          models={models}
          modelsLoading={modelsLoading}
          modelsError={modelsError}
          onClose={() => setSettingsOpen(false)}
          onLlmProviderChange={p => {
            setLlmProviderDraft(p);
            setModelDraft(
              p === "openai"
                ? (settings?.openai_model || "")
                : p === "anthropic"
                  ? (settings?.anthropic_model || "")
                  : p === "openrouter"
                    ? (settings?.openrouter_model || "")
                    : "",
            );
            void loadProviderModels(p);
          }}
          onBuildModeDraftChange={setBuildModeDraft}
          onModelDraftChange={setModelDraft}
          onApiKeyChange={(p, k) => {
            if (p === "openai") setOpenaiApiKeyDraft(k);
            else if (p === "anthropic") setAnthropicApiKeyDraft(k);
            else if (p === "openrouter") setOpenrouterApiKeyDraft(k);
          }}
          onLogout={logoutToStart}
          onSave={saveSettings}
        />
      </Suspense>

      <Topbar
        ws={ws}
        identity={identity}
        previewUrl={previewUrl}
        buildMode={buildMode}
        projects={projects}
        selectedProject={selectedProject}
        showExplorerPane={showExplorerPane}
        showAssistPane={showAssistPane}
        onQuickSwitchBuildMode={quickSwitchBuildMode}
        onPickWorkspace={pickWorkspace}
        onOpenSettings={openSettings}
        onSelectProject={setSelectedProject}
        onEnsurePreviewRunning={ensurePreviewRunning}
        onToggleExplorerPane={() => setShowExplorerPane((v) => !v)}
        onToggleAssistPane={() => setShowAssistPane((v) => !v)}
      />

      <div style={{ display: "none" }}>{hostedProjects.length}</div>
      <main className="appMain">
        <Suspense fallback={<div className="workspaceGateWrap"><div className="workspaceGateCard pane"><div className="workspaceGateTitle">Loading workspace…</div></div></div>}>
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
