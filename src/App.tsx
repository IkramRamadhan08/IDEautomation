import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import { Toaster, toast } from "sonner";
import { supabase } from "./lib/supabase";

import "./app.css";
import {
  agent,
  applyMany,
  detectProjects,
  getIdentity,
  getModels,
  getSettings,
  getUserPreferences,
  getProjectPreferences,
  listDir,
  readFile,
  runStart,
  pickWorkspaceNative,
  provisionWorkspace,
  resetClientIdentity,
  setWorkspace,
  updateSettings,
  updateUserPreferences,
  updateProjectPreferences,
  writeFile,
  importBrowserFolder,
  terminalRun,
  uploadImageAsset,
  validateProject,
  auditPreview,
  listHostedProjects,
  createHostedProject,
  type ProjectValidationRun,
  type PreviewAuditResult,
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
  type AgentChange,
  type UploadedImageAsset,
} from "./types";

import { Topbar } from "./components/navigation/Topbar";
import { AgentOrb } from "./components/agent/AgentOrb";
import { buildAgentRunPlan, buildRepairPrompt } from "./agent/modeOrchestration";

const SettingsModal = lazy(() => import("./components/settings/SettingsModal").then((module) => ({ default: module.SettingsModal })));
const HybridWorkspace = lazy(() => import("./modes/HybridWorkspace").then((module) => ({ default: module.HybridWorkspace })));
const FullAgentWorkspace = lazy(() => import("./modes/FullAgentWorkspace").then((module) => ({ default: module.FullAgentWorkspace })));

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

const DEMO_MODE_STORAGE_KEY = "voiceide-demo-mode";

function getDefaultAssistPaneWidth() {
  if (typeof window === "undefined") return 280;
  return Math.max(220, Math.min(280, Math.floor(window.innerWidth * 0.24)));
}

function getStoredDemoMode() {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(DEMO_MODE_STORAGE_KEY) === "1";
}

function setStoredDemoMode(enabled: boolean) {
  if (typeof window === "undefined") return;
  if (enabled) window.localStorage.setItem(DEMO_MODE_STORAGE_KEY, "1");
  else window.localStorage.removeItem(DEMO_MODE_STORAGE_KEY);
}

function isHostedBrowser() {
  if (typeof window === "undefined") return false;
  const host = window.location.hostname.toLowerCase();
  return !["localhost", "127.0.0.1", "::1"].includes(host);
}

function mergeBuffersWithChanges(currentBuffers: Record<string, FileBuffer>, changes: AgentChange[]) {
  let mutated = false;
  const next = { ...currentBuffers };

  for (const change of changes) {
    const existing = next[change.path];
    if (!existing) continue;
    next[change.path] = { content: change.new_content, dirty: false };
    mutated = true;
  }

  return mutated ? next : currentBuffers;
}

function formatValidationReport(validation: ProjectValidationRun, maxChars = 8000) {
  if (validation.results.length === 0) {
    return "No validation commands were inferred for this project.";
  }

  const report = validation.results
    .map((result, index) => {
      const chunks = [
        `#${index + 1} ${result.command}`,
        `status: ${result.ok ? "ok" : "failed"} (exit ${result.returncode})`,
      ];

      if (result.stdout.trim()) chunks.push(`stdout:\n${result.stdout.trim()}`);
      if (result.stderr.trim()) chunks.push(`stderr:\n${result.stderr.trim()}`);

      return chunks.join("\n");
    })
    .join("\n\n");

  return report.length > maxChars ? `${report.slice(0, maxChars)}\n…[truncated]` : report;
}

function formatPreviewAuditReport(audit: PreviewAuditResult, maxChars = 4000) {
  const sections = [
    `summary: ${audit.summary}`,
    audit.title ? `title: ${audit.title}` : "title: (missing)",
    audit.meta_description ? `meta: ${audit.meta_description}` : "meta: (missing)",
    audit.headings.length > 0 ? `headings: ${audit.headings.join(" | ")}` : "headings: (missing)",
    audit.buttons.length > 0 ? `buttons: ${audit.buttons.join(" | ")}` : "buttons: (none)",
    audit.issues.length > 0 ? `issues:\n- ${audit.issues.join("\n- ")}` : "issues: none",
  ];

  if (audit.excerpt.trim()) {
    sections.push(`excerpt:\n${audit.excerpt.trim()}`);
  }

  const report = sections.join("\n\n");
  return report.length > maxChars ? `${report.slice(0, maxChars)}\n…[truncated]` : report;
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

  // --- Auth & Init ---
  useEffect(() => {
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
      if (getStoredDemoMode()) {
        setGoogleAuth({
          ok: true,
          authenticated: true,
          phase: "demo",
          user: {
            sub: "demo-mode",
            email: null,
            name: "Demo Mode",
            picture: null,
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
    }
  }, [googleAuth?.authenticated]);

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
          if (nextProvider === "openai-codex") nextModel = prefs.openai_codex_model || s.openai_codex_model || "";
          else if (nextProvider === "anthropic") nextModel = prefs.anthropic_model || s.anthropic_model || "";
          else if (nextProvider === "openrouter") nextModel = prefs.openrouter_model || s.openrouter_model || "";
        } catch {
          // ignore hosted preference load failures and keep global settings fallback
        }
      }

      setBuildMode(nextBuildMode);
      setBuildModeDraft(nextBuildMode);
      setLlmProviderDraft(nextProvider);
      setModelDraft(nextModel || (nextProvider === "openai-codex" ? s.openai_codex_model : nextProvider === "anthropic" ? s.anthropic_model : nextProvider === "openrouter" ? s.openrouter_model : ""));
    } catch { /* ignore */ }
  };

  const loadIdentityOverview = async () => {
    try {
      const info = await getIdentity();
      setIdentity(info);
    } catch { /* ignore */ }
  };

  const startGoogleLogin = async () => {
    try {
      setStoredDemoMode(false);
      const { error } = await supabase.auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: window.location.origin },
      });
      if (error) throw error;
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  const startDemoMode = async () => {
    setStoredDemoMode(true);
    setGoogleAuth({
      ok: true,
      authenticated: true,
      phase: "demo",
      user: {
        sub: "demo-mode",
        email: null,
        name: "Demo Mode",
        picture: null,
      },
    });
    toast.success("Demo mode aktif");
  };

  const logoutToStart = async () => {
    try {
      setStoredDemoMode(false);
      await supabase.auth.signOut();
      resetClientIdentity();
      setWs(null);
      setIdentity(null);
    } catch (e) {
      toast.error(errorMessage(e));
    }
  };

  // --- Workspace & Files ---
  const refreshProjects = async () => {
    try {
      const [detected, hosted] = await Promise.all([
        detectProjects().catch(() => ({ ok: true, projects: [] as ProjectInfo[] })),
        listHostedProjects().catch(() => ({ ok: true, projects: [] as HostedProject[] })),
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
      if (!selectedProject || selectedProject === "." || !settings?.supabase_enabled || !googleAuth?.authenticated) return;
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
  }, [selectedProject, hostedProjects, settings?.supabase_enabled, googleAuth?.authenticated]);

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
    try {
      const picked = await pickWorkspaceNative();
      if (picked?.ok && picked.path) {
        const res = await setWorkspace(picked.path);
        setWs(res.path);
        return;
      }
    } catch { /* fall through */ }
    folderInputRef.current?.click();
  };

  const createManagedWorkspace = async () => {
    const res = await provisionWorkspace();
    setWs(res.path);
    setEditorStatus(`Workspace ready: ${res.path}`);
    await refreshProjects();
  };

  const createHostedProjectFromPrompt = async () => {
    const name = window.prompt("Project name:");
    if (!name?.trim()) return;
    try {
      const res = await createHostedProject({ name: name.trim() });
      if (settings?.supabase_enabled && googleAuth?.authenticated) {
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
    setModelsLoading(true);
    try {
      const res = await getModels(p);
      setModels(res.models || []);
      if (res.models?.length && !modelDraft) setModelDraft(res.models[0]);
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
        if (llmProviderDraft === "openai-codex") patch.openai_codex_model = modelDraft;
        else if (llmProviderDraft === "anthropic") patch.anthropic_model = modelDraft;
        else if (llmProviderDraft === "openrouter") patch.openrouter_model = modelDraft;
      }
      if (openaiApiKeyDraft) patch.openai_api_key = openaiApiKeyDraft;
      if (anthropicApiKeyDraft) patch.anthropic_api_key = anthropicApiKeyDraft;
      if (openrouterApiKeyDraft) patch.openrouter_api_key = openrouterApiKeyDraft;

      await updateSettings(patch);

      if (settings?.supabase_enabled && googleAuth?.authenticated) {
        await updateUserPreferences({
          llm_provider: llmProviderDraft || null,
          build_mode: buildModeDraft,
          openai_codex_model: llmProviderDraft === "openai-codex" ? modelDraft : null,
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
    if (!agentInput.trim() || agentStatus === "thinking") return;

    const runPlan = buildAgentRunPlan(buildMode, agentInput, previewUrl);
    const { requestEditorStatus, shouldDrivePreview, shouldRunValidation, shouldAuditPreview } = runPlan;
    let workingBuffers = buffers;
    let combinedActions: AgentAction[] = [];
    let finalStatus = "Agent task finished";
    let finalToast: { kind: "success" | "warning" | "error"; message: string } = {
      kind: "success",
      message: "Tugas selesai",
    };

    const syncBuffers = (changes: AgentChange[]) => {
      workingBuffers = mergeBuffersWithChanges(workingBuffers, changes);
      setBuffers(workingBuffers);
    };

    const appendLogSection = (label: string, content: string) => {
      if (!content.trim()) return;
      setAgentLog((prev) => `${prev}\n\n[${label}]\n${content}`);
    };

    const applyChanges = async (changes: AgentChange[], statusLabel = "Applying") => {
      if (changes.length === 0) return;
      setWorkingMsg(`Menerapkan ${changes.length} perubahan…`);
      setEditorStatus(`${statusLabel} ${changes.length} file changes...`);
      await applyMany(changes.map((c) => ({ path: c.path, content: c.new_content })), true);
      syncBuffers(changes);
      await refreshExplorer();
    };

    const runShellActionsForPass = async (actions: AgentAction[]) => {
      for (const action of actions) {
        if (action.type !== "shell" || typeof action.command !== "string") continue;
        setWorkingMsg(`Menjalankan: ${action.command}`);
        try {
          const runRes = await terminalRun(action.command, selectedProject !== "." ? selectedProject : undefined);
          appendLogSection(runRes.ok ? "TERMINAL STDOUT" : "TERMINAL STDERR", runRes.ok ? runRes.stdout : runRes.stderr);
        } catch (e) {
          appendLogSection("TERMINAL ERROR", errorMessage(e));
        }
      }
    };

    const refreshPreviewSurface = async () => {
      if (!shouldDrivePreview) return previewUrl || "";
      setWorkingMsg(previewUrl ? "Menyegarkan preview…" : "Menyalakan preview…");
      if (previewUrl) {
        setPreviewFrameKey((v) => v + 1);
        setEditorStatus("Preview refreshed after agent changes");
        return previewUrl;
      }
      return (await ensurePreviewRunning()) || "";
    };

    const runValidationPass = async (label: string) => {
      setWorkingMsg("Menjalankan validasi proyek…");
      const validation = await validateProject(selectedProject);
      appendLogSection(label, formatValidationReport(validation));
      return validation;
    };

    const runPreviewAuditPass = async (url: string, label: string) => {
      if (!url) return null;
      setWorkingMsg("Mengaudit preview yang lagi live…");
      const audit = await auditPreview(url);
      appendLogSection(label, formatPreviewAuditReport(audit));
      return audit;
    };

    setAgentStatus("thinking");
    setEditorStatus(requestEditorStatus);
    setWorkingMsg("Agent sedang berpikir…");
    try {
      const res = await agent(
        agentInput,
        activeFile || null,
        null,
        selectedProject,
        buildMode,
        attachedImage ? [attachedImage.path] : undefined,
        activeFile ? (workingBuffers[activeFile]?.content ?? null) : null,
        openFiles,
        previewUrl || null,
        requestEditorStatus,
      );
      setAgentReply(res.spoken);
      setAgentLog(res.log);

      const changes: AgentChange[] = res.changes || [];
      const actions: AgentAction[] = res.actions || [];
      combinedActions = [...actions];
      setAgentActions(combinedActions);

      if (changes.length > 0) {
        await applyChanges(changes);
      }

      if (actions.length > 0) {
        await runShellActionsForPass(actions);
      }

      const auditedPreviewUrl = changes.length > 0 ? await refreshPreviewSurface() : (previewUrl || "");

      let validation: ProjectValidationRun | null = null;
      let previewAudit: PreviewAuditResult | null = null;

      if ((changes.length > 0 || actions.length > 0) && shouldRunValidation) {
        validation = await runValidationPass("VALIDATION PASS 1");
      }
      if (auditedPreviewUrl && (changes.length > 0 || actions.length > 0) && shouldAuditPreview) {
        previewAudit = await runPreviewAuditPass(auditedPreviewUrl, "PREVIEW AUDIT 1");
      }

      const hasValidationIssues = Boolean(validation && !validation.ok);
      const hasPreviewIssues = Boolean(previewAudit && previewAudit.issues.length > 0);

      if ((hasValidationIssues || hasPreviewIssues) && changes.length > 0) {
        setWorkingMsg("Memperbaiki hasil audit…");
        setEditorStatus("Fixing preview and validation issues...");

        const validationReport = validation && !validation.ok ? formatValidationReport(validation, 6000) : null;
        const previewAuditReport = previewAudit && previewAudit.issues.length > 0 ? formatPreviewAuditReport(previewAudit, 3500) : null;

        const repairRes = await agent(
          buildRepairPrompt(buildMode, agentInput, validationReport, previewAuditReport),
          activeFile || null,
          null,
          selectedProject,
          buildMode,
          attachedImage ? [attachedImage.path] : undefined,
          activeFile ? (workingBuffers[activeFile]?.content ?? null) : null,
          openFiles,
          auditedPreviewUrl || previewUrl || null,
          "Fixing preview and validation issues...",
        );

        appendLogSection("REPAIR PASS", repairRes.log);
        if (repairRes.spoken) setAgentReply(repairRes.spoken);

        const repairChanges: AgentChange[] = repairRes.changes || [];
        const repairActions: AgentAction[] = repairRes.actions || [];
        combinedActions = [...combinedActions, ...repairActions];
        setAgentActions(combinedActions);

        if (repairChanges.length > 0) {
          await applyChanges(repairChanges, "Applying repair");
        }
        if (repairActions.length > 0) {
          await runShellActionsForPass(repairActions);
        }

        const repairedPreviewUrl = repairChanges.length > 0 ? await refreshPreviewSurface() : auditedPreviewUrl;
        const revalidation = shouldRunValidation ? await runValidationPass("VALIDATION PASS 2") : null;
        const reaudit = repairedPreviewUrl && shouldAuditPreview ? await runPreviewAuditPass(repairedPreviewUrl, "PREVIEW AUDIT 2") : null;

        const validationStillFailing = Boolean(revalidation && !revalidation.ok);
        const previewStillFailing = Boolean(reaudit && reaudit.issues.length > 0);

        if (!validationStillFailing && !previewStillFailing) {
          finalStatus = "Agent task finished, validated, and preview-audited";
          finalToast = { kind: "success", message: "Tugas selesai, lolos validasi, dan preview lebih rapi" };
        } else {
          finalStatus = previewStillFailing ? "Preview audit still found issues" : "Validation still failing";
          finalToast = { kind: "warning", message: "Perubahan diterapkan, tapi masih ada temuan audit yang perlu dicek" };
        }
      } else if (!hasValidationIssues && !hasPreviewIssues && (validation || previewAudit)) {
        finalStatus = previewAudit ? "Agent task finished, validated, and preview-audited" : "Agent task finished and validated";
        finalToast = {
          kind: "success",
          message: previewAudit ? "Tugas selesai, lolos validasi, dan preview diaudit" : "Tugas selesai dan lolos validasi",
        };
      } else if (changes.length > 0 && shouldDrivePreview) {
        finalStatus = auditedPreviewUrl ? "Preview refreshed after agent changes" : "Preview live after agent changes";
      }

      setEditorStatus(finalStatus);
      if (finalToast.kind === "success") toast.success(finalToast.message);
      else if (finalToast.kind === "warning") toast.warning(finalToast.message);
      else toast.error(finalToast.message);
      setAgentStatus("idle");
    } catch (e) {
      setEditorStatus("Agent failed");
      setAgentStatus("error");
      toast.error("Agent error: " + errorMessage(e));
    } finally {
      setWorkingMsg("");
    }
  };

  const ensurePreviewRunning = async () => {
    if (!ws) return "";
    if (isHostedBrowser()) {
      const msg = "Live preview lokal belum didukung di deployment Vercel. Untuk sekarang, mode web bisa edit dan agent dulu, tapi preview runtime harus dijalankan dari app lokal/desktop.";
      setEditorStatus("Preview lokal tidak tersedia di hosted mode");
      toast.error(msg);
      return "";
    }
    setEditorStatus(`Starting preview for ${selectedProject}...`);
    try {
      const r = await runStart(selectedProject);
      setPreviewUrl(r.url);
      setPreviewFrameKey((v) => v + 1);
      setEditorStatus(`Preview live at ${r.url}`);
      return r.url;
    } catch (e) {
      setEditorStatus("Failed to start preview");
      toast.error("Gagal menjalankan preview: " + errorMessage(e));
      return "";
    }
  };

  const quickSwitchBuildMode = (mode: BuildMode) => {
    setBuildMode(mode);
    void updateSettings({ build_mode: mode });
    if (settings?.supabase_enabled && googleAuth?.authenticated) {
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
          <button className="btn subtleBtn" onClick={() => void startDemoMode()}>Continue in Demo Mode</button>
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
          Open an existing project, or create a managed workspace and start from a cleaner default surface.
        </div>
        <div className="workspaceGateFeatureGrid">
          <div className="gateFeatureCard">
            <div className="gateFeatureTitle">Open existing folder</div>
            <div className="gateFeatureText">Best when you already have a repo and want to keep working immediately.</div>
          </div>
          <div className="gateFeatureCard">
            <div className="gateFeatureTitle">Create managed workspace</div>
            <div className="gateFeatureText">Best when you want an isolated place to scaffold and ship without clutter.</div>
          </div>
        </div>
        <div className="workspaceGateActions">
          <button className="btn primary" onClick={pickWorkspace}>Open folder…</button>
          <button className="btn" onClick={createManagedWorkspace}>Create workspace</button>
          <button className="btn" onClick={createHostedProjectFromPrompt}>New project</button>
          <button className="btn" onClick={logoutToStart}>Logout</button>
        </div>
        {hostedProjects.length > 0 ? (
          <div className="settingsSubtle" style={{ marginTop: 12 }}>
            Existing projects: {hostedProjects.map((project) => project.name).join(", ")}
          </div>
        ) : null}
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
          onLlmProviderChange={p => { setLlmProviderDraft(p); void loadProviderModels(p); }}
          onBuildModeDraftChange={setBuildModeDraft}
          onModelDraftChange={setModelDraft}
          onApiKeyChange={(p, k) => {
            if (p === "openai-codex") setOpenaiApiKeyDraft(k);
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

      <AgentOrb
        ws={ws}
        buildMode={buildMode}
        agentStatus={agentStatus}
        agentLog={agentLog}
        agentReply={agentReply}
        agentActions={agentActions}
        agentWidgetOpen={agentWidgetOpen}
        agentOrbPosition={agentOrbPosition}
        workingMsg={workingMsg}
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
        onSetPosition={setAgentOrbPosition}
      />
    </div>
  );
}
