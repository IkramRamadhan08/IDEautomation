import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
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
  listProjectTemplates,
  createHostedProject,
  listCheckpoints,
  restoreCheckpoint,
  type HostedProject,
  type ProjectTemplate,
  type UserPreferences,
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

  const projectOptions = hostedProjects.length > 0
    ? hostedProjects.map((project) => ({ root: project.root, name: project.name }))
    : projects.map((project) => ({ root: project.root, name: project.name }));
  const [agentRunViewPinned, setAgentRunViewPinned] = useState(false);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [projectTemplates, setProjectTemplates] = useState<ProjectTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState("saas-dashboard");
  const [newProjectSaving, setNewProjectSaving] = useState(false);
  const [settings, setSettings] = useState<SettingsInfo | null>(null);
  const [llmProviderDraft, setLlmProviderDraft] = useState<ProviderChoice>("");
  const [buildMode, setBuildMode] = useState<BuildMode>("hybrid");
  const [buildModeDraft, setBuildModeDraft] = useState<BuildMode>("hybrid");
  const [modelDraft, setModelDraft] = useState<string>("");
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

  const modelFromPreferences = (provider: ProviderChoice, prefs: UserPreferences | null): string => {
    if (!prefs) return "";
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
          nextModel = modelFromPreferences(nextProvider, prefs) || modelFromSettings(nextProvider, s);
        } catch {
          // ignore hosted preference load failures and keep global settings fallback
        }
      }

      setBuildMode(nextBuildMode);
      setBuildModeDraft(nextBuildMode);
      setLlmProviderDraft(nextProvider);
      setModelDraft(nextModel || modelFromSettings(nextProvider, s));
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
      const [detected, hosted, templates] = await Promise.all([
        detectProjects().catch(() => ({ ok: true, projects: [] as ProjectInfo[] })),
        hasVerifiedHostedAuth ? listHostedProjects().catch(() => ({ ok: true, projects: [] as HostedProject[] })) : Promise.resolve({ ok: true, projects: [] as HostedProject[] }),
        hasVerifiedHostedAuth ? listProjectTemplates().catch(() => ({ ok: true, templates: [] as ProjectTemplate[] })) : Promise.resolve({ ok: true, templates: [] as ProjectTemplate[] }),
      ]);
      setProjects(detected.projects || []);
      setHostedProjects(hosted.projects || []);
      setProjectTemplates(templates.templates || []);
    } catch {
      setProjects([]);
      setHostedProjects([]);
      setProjectTemplates([]);
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
    setPreviewUrl("");
  };

  const openSavedProject = async (projectRoot: string) => {
    try {
      if (!ws) {
        const provisioned = await provisionWorkspace();
        setWs(provisioned.path);
      }
      selectProject(projectRoot);
      setEditorStatus(`Project ready: ${projectRoot}`);
      toast.success("Project dibuka");
    } catch (e) {
      toast.error("Gagal membuka project: " + errorMessage(e));
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
        llm_provider: llmProviderDraft,
        build_mode: buildModeDraft,
      };
      if (modelDraft) {
        if (llmProviderDraft === "openai") patch.openai_model = modelDraft;
        else if (llmProviderDraft === "anthropic") patch.anthropic_model = modelDraft;
        else if (llmProviderDraft === "openrouter") patch.openrouter_model = modelDraft;
        else if (llmProviderDraft === "groq") patch.groq_model = modelDraft;
        else if (llmProviderDraft === "gemini") patch.gemini_model = modelDraft;
        else if (llmProviderDraft === "together") patch.together_model = modelDraft;
        else if (llmProviderDraft === "cerebras") patch.cerebras_model = modelDraft;
        else if (llmProviderDraft === "xai") patch.xai_model = modelDraft;
      }
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
          llm_provider: llmProviderDraft || null,
          build_mode: buildModeDraft,
          openai_model: llmProviderDraft === "openai" ? modelDraft : null,
          anthropic_model: llmProviderDraft === "anthropic" ? modelDraft : null,
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
  const renderGoogleLoginGate = () => (
    <div className="authLanding">
      <header className="authLandingNav">
        <div className="authBrand">
          <span className="authBrandMark">A</span>
          <span>Appora</span>
        </div>
        <nav className="authNavLinks" aria-label="Landing navigation">
          <a href="#faq">FAQ</a>
          <a href="#docs">Docs</a>
          <a href="#tutorial">Tutorial</a>
          <a href="#templates">Templates</a>
        </nav>
      </header>

      <main className="authLandingMain">
        <section className="authHeroSection">
          <div className="authHeroCopy">
          <div className="workspaceGateKicker">Agentic web builder for everyone</div>
          <h1 className="authHeroTitle">Appora</h1>
          <p className="authHeroSubtitle">
            Create web apps by talking to Clara, then refine the result in a browser IDE with files, terminal actions, preview checks, project memory, and provider fallback.
          </p>
          <div className="authHeroActions">
            <button className="btn primary authPrimaryCta" onClick={startGoogleLogin}>Continue with Google</button>
            <span className="authHeroNote">Hosted on Vercel. Supabase projects. Bring your own model key.</span>
          </div>
          <div className="authHeroBullets">
            <span>Start from production-ready templates</span>
            <span>Let the agent run, inspect, repair, and remember</span>
            <span>Use OpenRouter, Gemini, Groq, OpenAI, and more</span>
          </div>
          <div className="authHeroSignalGrid" aria-label="Appora capabilities">
            <div>
              <span>Template first</span>
              <strong>SaaS, landing, admin, AI tool</strong>
            </div>
            <div>
              <span>Agent loop</span>
              <strong>plan, patch, run, repair</strong>
            </div>
            <div>
              <span>Hosting target</span>
              <strong>Vercel + Supabase</strong>
            </div>
            <div>
              <span>Model routing</span>
              <strong>free-tier friendly BYOK</strong>
            </div>
          </div>
          <div className="authMetricRow">
            <div>
              <strong>8</strong>
              <span>providers</span>
            </div>
            <div>
              <strong>2</strong>
              <span>agent modes</span>
            </div>
            <div>
              <strong>0</strong>
              <span>local setup</span>
            </div>
          </div>
          </div>

        <section className="authProductPreview" aria-label="Appora workspace preview">
          <div className="authPreviewChrome">
            <span />
            <span />
            <span />
            <div>appora.app/workspace</div>
          </div>
          <div className="authPreviewGrid">
            <div className="authPreviewRail">
              <div className="authPreviewRailTitle">Files</div>
              <div className="authPreviewFile active">src/App.tsx</div>
              <div className="authPreviewFile">src/app.css</div>
              <div className="authPreviewFile">project memory</div>
              <div className="authPreviewFile">package.json</div>
            </div>
            <div className="authPreviewEditor">
              <div className="authPreviewTab">Clara is building a SaaS dashboard</div>
              <div className="authCodeLine w80" />
              <div className="authCodeLine w60" />
              <div className="authCodeLine w90" />
              <div className="authCodeLine w45" />
              <div className="authCodeBlock" />
              <div className="authCodeLine w70" />
              <div className="authCodeLine w52" />
            </div>
            <div className="authPreviewAssist">
              <div className="authAssistLabel">Live agent</div>
              <div className="authAssistBubble">Clara applied 6 patches, ran build, checked mobile preview, and repaired a layout overflow.</div>
              <div className="authAssistTrace">template: SaaS Dashboard</div>
              <div className="authAssistTrace">terminal: npm run build</div>
              <div className="authAssistTrace">browser audit: passed</div>
            </div>
          </div>
        </section>
        </section>

        <section className="authInfoBand authCapabilityBand" aria-label="What Appora can build">
          <div className="authSectionHeader">
            <span>What you can build</span>
            <h2>Start with a real app shape, then let the agent push it toward something usable.</h2>
          </div>
          <div className="authCapabilityGrid">
            <div className="wide">
              <strong>Business apps</strong>
              <p>Dashboards, client portals, admin panels, internal tools, analytics views, and CRUD flows with real empty, loading, and error states.</p>
            </div>
            <div>
              <strong>Public sites</strong>
              <p>Landing pages, pricing pages, waitlists, portfolios, product pages, and campaign sites with responsive sections.</p>
            </div>
            <div>
              <strong>AI products</strong>
              <p>Prompt tools, chat interfaces, generation workflows, BYOK settings, and model routing for users testing free tiers.</p>
            </div>
            <div>
              <strong>Prototype to MVP</strong>
              <p>Use templates to get structure fast, then ask Clara to add routes, polish UI, wire state, and validate the build.</p>
            </div>
            <div className="wide">
              <strong>Hosted project workspaces</strong>
              <p>Projects are meant to live in the browser with Supabase persistence, preview checks, checkpoints, and a coding surface for deeper edits.</p>
            </div>
          </div>
        </section>

        <section className="authInfoBand authAgentBand" aria-label="Agent capabilities">
          <div className="authSectionHeader">
            <span>Agent capabilities</span>
            <h2>Clara builds autonomously. Raka helps when the user wants precise control.</h2>
          </div>
          <div className="authAgentGrid">
            <div>
              <span>Clara</span>
              <strong>Full builder agent</strong>
              <p>Understands the request, inspects files, makes scoped patches, requests terminal actions, reads failures, repairs, and summarizes the result.</p>
            </div>
            <div>
              <span>Raka</span>
              <strong>Hybrid IDE copilot</strong>
              <p>Works close to the active file and editor context for focused changes, refactors, explanations, and incremental implementation.</p>
            </div>
            <div>
              <span>Memory</span>
              <strong>Project-aware context</strong>
              <p>Stores project profile, stack signals, decisions, recent runs, and reusable facts so follow-up requests stay connected.</p>
            </div>
            <div>
              <span>Tools</span>
              <strong>Terminal, MCP, preview audit</strong>
              <p>Can use local repo tools, MCP integrations, shell actions, browser inspection, and build checks when those steps matter.</p>
            </div>
          </div>
        </section>

        <section className="authInfoBand authWorkspaceBand" aria-label="Workspace overview">
          <div className="authSectionHeader">
            <span>Inside the workspace</span>
            <h2>A browser IDE shaped for non-coders, with enough control for serious iteration.</h2>
          </div>
          <div className="authWorkspaceGrid">
            <div>
              <strong>Files</strong>
              <p>Browse generated app files, open tabs, edit code, and keep project structure visible.</p>
            </div>
            <div>
              <strong>Preview</strong>
              <p>Run the app preview, inspect page health, and catch layout or runtime problems before shipping.</p>
            </div>
            <div>
              <strong>Terminal actions</strong>
              <p>Let the agent request installs, builds, tests, and scripts while actions stay separated from chat output.</p>
            </div>
            <div>
              <strong>Checkpoints</strong>
              <p>Track patch metadata and repair attempts so larger tasks do not feel like blind one-shot generations.</p>
            </div>
          </div>
        </section>

        <section id="tutorial" className="authInfoBand" aria-label="Tutorial">
          <div className="authSectionHeader">
            <span>Tutorial</span>
            <h2>From blank idea to working app without setting up a repo.</h2>
          </div>
          <div className="authFeatureGrid">
            <div className="authFeatureCard">
              <strong>1. Pick a starter</strong>
              <p>Choose SaaS, landing, admin CRUD, or AI tool templates with real routes, states, and project memory.</p>
            </div>
            <div className="authFeatureCard">
              <strong>2. Talk to Clara</strong>
              <p>Describe the product in plain language. Clara plans, edits files, runs commands, and repairs failures.</p>
            </div>
            <div className="authFeatureCard">
              <strong>3. Refine in the IDE</strong>
              <p>Use Raka for focused edits while preview audit, checkpoints, and Supabase persistence keep the work grounded.</p>
            </div>
          </div>
        </section>

        <section id="templates" className="authInfoBand authTemplateShowcase" aria-label="Templates">
          <div className="authSectionHeader">
            <span>Starter library</span>
            <h2>Production-oriented templates, not empty demo folders.</h2>
          </div>
          <div className="authTemplateList">
            <span>SaaS Dashboard</span>
            <span>Landing + Pricing</span>
            <span>Admin CRUD</span>
            <span>AI Tool App</span>
          </div>
        </section>

        <section id="docs" className="authInfoBand" aria-label="Docs">
          <div className="authSectionHeader">
            <span>Docs</span>
            <h2>Built around hosted projects, provider keys, agent memory, and preview checks.</h2>
          </div>
          <div className="authDocsGrid">
            <div>
              <strong>Hosted setup</strong>
              <p>Projects persist through Supabase, while serverless API routes handle auth, settings, files, and agent runs.</p>
            </div>
            <div>
              <strong>Agent tools</strong>
              <p>Clara can inspect project files, request terminal actions, use MCP tools, remember project context, and repair failed builds.</p>
            </div>
            <div>
              <strong>Provider routing</strong>
              <p>Users paste their own keys and Appora picks connected models with fallback paths for rate limits.</p>
            </div>
            <div>
              <strong>Preview confidence</strong>
              <p>Browser inspection catches blank pages, runtime errors, layout overflow, missing assets, and mobile breakage.</p>
            </div>
          </div>
        </section>

        <section id="providers" className="authInfoBand authProviderBand" aria-label="Providers">
          <div className="authSectionHeader">
            <span>BYOK model routing</span>
            <h2>Paste the key you already have. Appora can route and fallback when limits hit.</h2>
          </div>
          <div className="authProviderList">
            {["OpenRouter", "Gemini", "Groq", "OpenAI", "Anthropic", "Together", "Cerebras", "xAI"].map((provider) => (
              <span key={provider}>{provider}</span>
            ))}
          </div>
        </section>

        <section id="faq" className="authInfoBand" aria-label="FAQ">
          <div className="authSectionHeader">
            <span>FAQ</span>
            <h2>For users who want to build, not configure a local dev machine.</h2>
          </div>
          <div className="authFaqList">
            <div>
              <strong>Do I need to code?</strong>
              <p>No. Start with a template, describe the app, and use the IDE only when you want more control.</p>
            </div>
            <div>
              <strong>Do I need paid AI credits?</strong>
              <p>You bring your own provider keys. Free and trial tiers can work, but limits depend on each provider.</p>
            </div>
            <div>
              <strong>Where are projects stored?</strong>
              <p>Hosted projects are designed to persist through Supabase, with Vercel serving the app experience.</p>
            </div>
          </div>
        </section>
      </main>
    </div>
  );

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

  const renderWorkspaceOnboarding = () => (
    <div className="workspaceGateWrap workspaceSetupWrap">
      {renderNewProjectModal()}
      <div className="workspaceGateCard pane workspaceSetupCard">
        <div className="workspaceGateKicker">Project setup</div>
        <div className="workspaceGateTitle">Start a project for this session</div>
        <div className="workspaceGateSubtitle">
          Open an existing project, upload one, or create a new Supabase-backed project for the agent.
          {isHostedBrowser() ? " Project text files are restored from Supabase between serverless runs." : ""}
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
        <div className="workspaceGateActions">
          <button className="btn primary" onClick={pickWorkspace}>{isHostedBrowser() ? "Upload project…" : "Open project…"}</button>
          <button className="btn" onClick={() => setNewProjectOpen(true)}>New project</button>
          <button className="btn" onClick={logoutToStart}>Logout</button>
        </div>
        {hasVerifiedHostedAuth && hostedProjects.length > 0 ? (
          <div className="savedProjectsPanel">
            <div className="savedProjectsHeader">
              <div>
                <div className="savedProjectsEyebrow">Saved projects</div>
                <div className="savedProjectsTitle">Project yang pernah dibuat</div>
              </div>
              <button className="btn subtleBtn" onClick={() => void refreshProjects()}>Refresh</button>
            </div>
            <div className="savedProjectsList">
              {hostedProjects.map((project) => (
                <button
                  key={project.id}
                  className="savedProjectItem"
                  onClick={() => void openSavedProject(project.root)}
                >
                  <span>
                    <strong>{project.name}</strong>
                    <small>{project.root}</small>
                  </span>
                  <span className="savedProjectOpen">Open</span>
                </button>
              ))}
            </div>
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
            setLlmProviderDraft(p);
            setModelDraft(modelFromSettings(p, settings));
            void loadProviderModels(p);
          }}
          onBuildModeDraftChange={setBuildModeDraft}
          onModelDraftChange={setModelDraft}
          onApiKeyChange={(p, k) => {
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

      <Topbar
        ws={ws}
        identity={identity}
        previewUrl={previewUrl}
        buildMode={buildMode}
        showExplorerPane={showExplorerPane}
        showAssistPane={showAssistPane}
        onQuickSwitchBuildMode={quickSwitchBuildMode}
        onOpenSettings={openSettings}
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
