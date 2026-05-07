import { lazy, Suspense, useCallback, useEffect, useRef, useState, type FormEvent, type MouseEvent as ReactMouseEvent } from "react";
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
import { ArrowRight, Bot, BookOpen, Boxes, Moon, PanelsTopLeft, Rocket, Sparkles, Sun, Workflow } from "lucide-react";
import { runAgentWorkflow } from "./agent/workflow";
import { errorMessage, notifyToast } from "./app/feedback";
import { ensurePreviewRunningFlow, isHostedBrowser } from "./preview/runtime";

const SettingsModal = lazy(() => import("./components/settings/SettingsModal").then((module) => ({ default: module.SettingsModal })));
const HybridWorkspace = lazy(() => import("./modes/HybridWorkspace").then((module) => ({ default: module.HybridWorkspace })));
const FullAgentWorkspace = lazy(() => import("./modes/FullAgentWorkspace").then((module) => ({ default: module.FullAgentWorkspace })));

const MODEL_PROVIDERS = ["OpenRouter", "Gemini", "Groq", "OpenAI", "Anthropic", "Together", "Cerebras", "xAI"];
type AppTheme = "light" | "dark";

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
  const [appTheme, setAppTheme] = useState<AppTheme>(() => {
    if (typeof window === "undefined") return "light";
    const saved = window.localStorage.getItem("appora-theme");
    return saved === "dark" || saved === "light" ? saved : "light";
  });
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

  useEffect(() => {
    document.documentElement.dataset.appTheme = appTheme;
    window.localStorage.setItem("appora-theme", appTheme);
  }, [appTheme]);

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

  const scrollLandingTo = (event: ReactMouseEvent<HTMLElement>, sectionId: string) => {
    event.preventDefault();
    const scroller = document.querySelector<HTMLElement>(".authLanding");
    const target = sectionId === "top" ? scroller : document.getElementById(sectionId);
    if (!scroller || !target) return;

    const top = sectionId === "top" ? 0 : target.offsetTop - 84;

    scroller.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
    window.history.replaceState(null, "", `#${sectionId}`);
  };

  // --- Renders ---
  const renderGoogleLoginGate = () => (
    <div className="authLanding splineLanding" id="top">
      <header className="authLandingNav splineNav">
        <button className="splineBrandButton" type="button" onClick={(event) => scrollLandingTo(event, "top")} aria-label="Back to top">
          <span className="authBrandMark">A</span>
          <span>Appora</span>
        </button>
        <nav className="authNavLinks" aria-label="Landing navigation">
          <a href="#build" onClick={(event) => scrollLandingTo(event, "build")}>Explore</a>
          <a href="#agents" onClick={(event) => scrollLandingTo(event, "agents")}>Agents</a>
          <a href="#templates" onClick={(event) => scrollLandingTo(event, "templates")}>Templates</a>
          <a href="#tutorial" onClick={(event) => scrollLandingTo(event, "tutorial")}>Workflow</a>
          <a href="#docs" onClick={(event) => scrollLandingTo(event, "docs")}>Resources</a>
        </nav>
        <div className="splineNavActions">
          <button className="splineThemeToggle" type="button" onClick={() => setAppTheme((theme) => theme === "dark" ? "light" : "dark")} title={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`} aria-label={`Switch to ${appTheme === "dark" ? "light" : "dark"} mode`}>
            {appTheme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>
          <button className="splineNavCta" onClick={startGoogleLogin}>
            Start building
            <ArrowRight size={16} />
          </button>
        </div>
      </header>

      <main className="splineLandingMain">
        <section className="splineHeroSection">
          <div className="splineHeroCopy">
            <div className="splineKicker"><Sparkles size={15} /> Agentic app builder community</div>
            <h1>Discover, remix, and ship web apps with AI agents.</h1>
            <p>
              Appora turns templates, model keys, project memory, terminal actions, and preview checks into a hosted builder for people who want results without local setup.
            </p>
            <div className="splineHeroActions">
              <button className="splinePrimaryButton" onClick={startGoogleLogin}>
                Continue with Google
                <ArrowRight size={17} />
              </button>
              <button className="splineSecondaryButton" onClick={(event) => scrollLandingTo(event, "templates")}>
                Browse templates
              </button>
            </div>
            <div className="splineHeroStats" aria-label="Appora highlights">
              <span><strong>8</strong> model providers</span>
              <span><strong>2</strong> agent modes</span>
              <span><strong>0</strong> local setup</span>
            </div>
          </div>

          <div className="splineHeroVisual" aria-label="Appora visual workspace preview">
            <div className="splineSceneGrid">
              <div className="splineSceneCard sceneLarge">
                <span>Clara</span>
                <strong>Building SaaS dashboard</strong>
                <p>6 patches applied. Build passed. Mobile preview repaired.</p>
              </div>
              <div className="splineSceneCard sceneCyan"><Bot size={26} /><span>Agent loop</span></div>
              <div className="splineSceneCard scenePink"><PanelsTopLeft size={26} /><span>Live preview</span></div>
              <div className="splineSceneCard sceneLime"><Workflow size={26} /><span>Project memory</span></div>
              <div className="splineSceneCard sceneDark">
                <code>npm run build</code>
                <small>terminal action</small>
              </div>
            </div>
          </div>
        </section>

        <section className="splineProviderMarquee" aria-label="Supported model providers">
          <span>Connect model keys users already have</span>
          <div className="authMarqueeViewport">
            <div className="authMarqueeTrack">
              {[...MODEL_PROVIDERS, ...MODEL_PROVIDERS].map((provider, index) => (
                <span className="authProviderLogoChip" key={`${provider}-primary-${index}`}>
                  <em>{provider.slice(0, 2)}</em>
                  {provider}
                </span>
              ))}
            </div>
          </div>
        </section>

        <section id="build" className="splineSection splineExploreSection" aria-label="Explore Appora">
          <div className="splineSectionHeader centered">
            <span>Explore Appora</span>
            <h2>Pick a direction, then let the agent turn it into a working project.</h2>
          </div>
          <div className="splineCategoryTabs" aria-label="Build categories">
            <span>All Resources</span>
            <span>Dashboards</span>
            <span>Landing pages</span>
            <span>AI tools</span>
            <span>Admin apps</span>
          </div>
          <div className="splineGalleryGrid">
            <article className="splineGalleryCard cardMint">
              <Boxes size={30} />
              <span>Template</span>
              <h3>SaaS command center</h3>
              <p>Billing, analytics, users, settings, and responsive dashboard states.</p>
            </article>
            <article className="splineGalleryCard cardBlue tall">
              <Rocket size={30} />
              <span>Starter</span>
              <h3>Launch page with pricing</h3>
              <p>Hero, feature grid, pricing, FAQ, final CTA, and production-ready copy sections.</p>
            </article>
            <article className="splineGalleryCard cardYellow">
              <PanelsTopLeft size={30} />
              <span>Workspace</span>
              <h3>Browser IDE</h3>
              <p>Files, editor, preview, terminal actions, and checkpoints in one hosted surface.</p>
            </article>
            <article className="splineGalleryCard cardPink wide">
              <Sparkles size={30} />
              <span>AI product</span>
              <h3>Prompt app with BYOK routing</h3>
              <p>Provider settings, chat flow, model fallback, and user-owned keys for free-tier experimentation.</p>
            </article>
          </div>
        </section>

        <section id="agents" className="splineSection splineAgentsSection" aria-label="Agents">
          <div className="splineSectionHeader">
            <span>Agent studio</span>
            <h2>Two builders with different personalities and responsibilities.</h2>
          </div>
          <div className="splineAgentPanels">
            <article>
              <div className="splineAgentBadge clara">Clara</div>
              <h3>Full builder agent</h3>
              <p>Plans, patches, requests terminal actions, reads failures, repairs, and summarizes the result in the orb.</p>
              <ul>
                <li>Patch-based editing</li>
                <li>Preview inspection</li>
                <li>Project memory</li>
              </ul>
            </article>
            <article>
              <div className="splineAgentBadge raka">Raka</div>
              <h3>Hybrid IDE copilot</h3>
              <p>Works close to the active file when users want direct control, focused edits, and code explanations.</p>
              <ul>
                <li>Editor-aware help</li>
                <li>Scoped refactors</li>
                <li>Workspace context</li>
              </ul>
            </article>
          </div>
        </section>

        <section id="templates" className="splineSection" aria-label="Templates">
          <div className="splineSectionHeader centered">
            <span>Templates</span>
            <h2>Not blank projects. App shapes users can remix immediately.</h2>
          </div>
          <div className="splineTemplateRows">
            {["SaaS Dashboard", "Landing + Pricing", "Admin CRUD", "AI Tool App", "Portfolio", "Client Portal"].map((template) => (
              <div className="splineTemplateTile" key={template}>
                <strong>{template}</strong>
                <span>Open, remix, ask Clara to customize</span>
              </div>
            ))}
          </div>
        </section>

        <section id="tutorial" className="splineSection splineWorkflowSection" aria-label="Workflow">
          <div className="splineSectionHeader">
            <span>Workflow</span>
            <h2>From rough idea to previewable app without asking users to configure a machine.</h2>
          </div>
          <div className="splineWorkflowRail">
            <div><em>01</em><strong>Choose a base</strong><p>Start from a template that already has routes, states, and layout structure.</p></div>
            <div><em>02</em><strong>Talk to Clara</strong><p>Describe the product. Clara edits files, asks for actions, and repairs failures.</p></div>
            <div><em>03</em><strong>Inspect and refine</strong><p>Use preview checks, terminal output, and Raka for focused corrections.</p></div>
            <div><em>04</em><strong>Keep context</strong><p>Project memory stores stack signals, decisions, and recent implementation facts.</p></div>
          </div>
        </section>

        <section id="providers" className="splineSection splineProvidersSection" aria-label="Providers">
          <div className="splineSectionHeader centered">
            <span>Provider routing</span>
            <h2>BYOK for people who paste the key they already have.</h2>
          </div>
          <div className="splineProviderCloud">
            {MODEL_PROVIDERS.map((provider) => (
              <span key={provider}><em>{provider.slice(0, 2)}</em>{provider}</span>
            ))}
          </div>
        </section>

        <section id="docs" className="splineSection splineResourcesSection" aria-label="Resources">
          <div className="splineSectionHeader">
            <span>Resources</span>
            <h2>Clear enough for beginners, structured enough for serious app work.</h2>
          </div>
          <div className="splineResourceGrid">
            <article><BookOpen size={24} /><strong>Hosted setup</strong><p>Vercel serverless plus Supabase-backed project records, settings, keys, and memory.</p></article>
            <article><Workflow size={24} /><strong>Action model</strong><p>Conversation, patches, terminal actions, tool calls, and preview checks stay separated.</p></article>
            <article><Bot size={24} /><strong>MCP and skills</strong><p>Advanced users can connect external tools without forcing first-time users through setup.</p></article>
            <article><Rocket size={24} /><strong>Preview confidence</strong><p>Catch blank screens, runtime errors, layout overflow, and failed builds before users trust the result.</p></article>
          </div>
        </section>

        <section id="faq" className="splineSection splineFaqSection" aria-label="FAQ">
          <div className="splineSectionHeader centered">
            <span>FAQ</span>
            <h2>The basics users need before they start.</h2>
          </div>
          <div className="splineFaqList">
            <details open><summary>Do I need to code?</summary><p>No. Start with a template and ask Clara to build. The IDE is there when you want more control.</p></details>
            <details><summary>Do I need paid AI credits?</summary><p>Users bring their own keys. Free and trial tiers can work, but limits depend on the provider.</p></details>
            <details><summary>Where are projects stored?</summary><p>The hosted product is designed around Supabase persistence and Vercel delivery.</p></details>
            <details><summary>Can I still edit code myself?</summary><p>Yes. Raka keeps the hybrid IDE workflow available for direct edits and focused assistance.</p></details>
          </div>
        </section>

        <section className="splineFinalCta" aria-label="Start building">
          <span>The community is waiting for your next app</span>
          <h2>Open Appora and turn the rough idea into a project Clara can actually work on.</h2>
          <button className="splinePrimaryButton" onClick={startGoogleLogin}>
            Continue with Google
            <ArrowRight size={17} />
          </button>
        </section>
      </main>

      <footer className="authFooter splineFooter">
        <div className="authFooterBrand">
          <button className="splineBrandButton" type="button" onClick={(event) => scrollLandingTo(event, "top")}>
            <span className="authBrandMark">A</span>
            <span>Appora</span>
          </button>
          <p>Agentic web builder for hosted Vercel and Supabase app workflows.</p>
        </div>
        <div className="authFooterGrid">
          <div>
            <strong>Product</strong>
            <a href="#build" onClick={(event) => scrollLandingTo(event, "build")}>Explore</a>
            <a href="#agents" onClick={(event) => scrollLandingTo(event, "agents")}>Agents</a>
            <a href="#templates" onClick={(event) => scrollLandingTo(event, "templates")}>Templates</a>
          </div>
          <div>
            <strong>Resources</strong>
            <a href="#tutorial" onClick={(event) => scrollLandingTo(event, "tutorial")}>Workflow</a>
            <a href="#docs" onClick={(event) => scrollLandingTo(event, "docs")}>Resources</a>
            <a href="#faq" onClick={(event) => scrollLandingTo(event, "faq")}>FAQ</a>
          </div>
          <div>
            <strong>Platform</strong>
            <span>BYOK providers</span>
            <span>Project memory</span>
            <span>Preview audit</span>
          </div>
        </div>
        <div className="authFooterBottom">
          <span>Appora</span>
          <span>Discover, remix, and ship web apps with AI agents.</span>
        </div>
      </footer>
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
        appTheme={appTheme}
        showExplorerPane={showExplorerPane}
        showAssistPane={showAssistPane}
        onQuickSwitchBuildMode={quickSwitchBuildMode}
        onToggleTheme={() => setAppTheme((theme) => theme === "dark" ? "light" : "dark")}
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
