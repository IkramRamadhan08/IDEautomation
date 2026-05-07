import type { Dispatch, SetStateAction } from "react";
import {
  applyMany,
  auditPreview,
  fetchAgentCapabilities,
  readFile,
  streamAgent,
  terminalRun,
  validateProject,
  type AgentIntent,
  type AgentRunTrace,
  type PreviewAuditResult,
  type ProjectValidationRun,
  type TerminalRunResult,
} from "../api";
import type { AgentAction, AgentAuditSnapshot, AgentChange, AgentLiveItem, BuildMode, FileBuffer } from "../types";
import { buildRepairPrompt, buildVerifierRepairPrompt, getAgentRunPlan } from "./runtime";

type Setter<T> = Dispatch<SetStateAction<T>>;

type ToastKind = "success" | "warning" | "error";

type AgentStatus = "idle" | "thinking" | "error";

type WorkflowToast = {
  kind: ToastKind;
  message: string;
};

type ShellActionRun = TerminalRunResult & {
  command: string;
  error?: string;
};

type WorkflowArgs = {
  agentInput: string;
  agentStatus: AgentStatus;
  buildMode: BuildMode;
  friendlyFreeTierMode: boolean;
  previewUrl: string;
  selectedProject: string;
  attachedImagePath?: string | null;
  activeFile: string;
  openFiles: string[];
  buffers: Record<string, FileBuffer>;
  makeAgentLiveId: () => string;
  pushAgentLiveItem: (item: Omit<AgentLiveItem, "id">) => void;
  appendAssistantLiveText: (chunk: string, tone?: AgentLiveItem["tone"]) => void;
  refreshExplorer: (path?: string) => Promise<void>;
  ensurePreviewRunning: () => Promise<string>;
  refreshPreviewFrame: () => void;
  notify: (toast: WorkflowToast) => void;
  errorMessage: (error: unknown) => string;
  setBuffers: Setter<Record<string, FileBuffer>>;
  setAgentStatus: Setter<AgentStatus>;
  setAgentWidgetOpen: Setter<boolean>;
  setAgentRunViewPinned: Setter<boolean>;
  setAgentReply: Setter<string>;
  setAgentLog: Setter<string>;
  setAgentActions: Setter<AgentAction[]>;
  setAgentLiveItems: Setter<AgentLiveItem[]>;
  setAgentAuditTrail: Setter<AgentAuditSnapshot[]>;
  setEditorStatus: Setter<string>;
  setWorkingMsg: Setter<string>;
};

const PHASE_LABELS: Record<string, string> = {
  queued: "Masuk antrean kerja…",
  starting: "Nyusun konteks kerja…",
  intent: "Ngebedain intent dulu…",
  memory: "Ngambil memory yang relevan…",
  skills: "Milih skill yang paling kepake…",
  mcp: "Ngecek integrasi MCP yang tersedia…",
  planning: "Nyusun rencana kerja…",
  context_ready: "Konteks siap, mulai ngerjain…",
  drafting: "Lagi nyusun respons…",
  tooling: "Lagi jalanin tool agent…",
  refining: "Lagi merapikan hasil…",
  verifying: "Ngecek hasil agent…",
  diffing: "Lagi nyusun patch yang rapi…",
};

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

function checkpointPathForRun(selectedProject: string) {
  const suffix = `.voiceide/checkpoints/${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  return selectedProject !== "." ? `${selectedProject}/${suffix}` : suffix;
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

function formatShellActionReport(results: ShellActionRun[], maxChars = 8000) {
  if (results.length === 0) return "";

  const report = results
    .map((result, index) => {
      const chunks = [
        `#${index + 1} ${result.command}`,
        `status: ${result.ok ? "ok" : "failed"} (exit ${result.returncode})`,
      ];

      if (result.error?.trim()) chunks.push(`execution_error:\n${result.error.trim()}`);
      if (result.stdout.trim()) chunks.push(`stdout:\n${result.stdout.trim()}`);
      if (result.stderr.trim()) chunks.push(`stderr:\n${result.stderr.trim()}`);
      if (typeof result.synced_files === "number") chunks.push(`synced_files: ${result.synced_files}`);

      return chunks.join("\n");
    })
    .join("\n\n");

  return report.length > maxChars ? `${report.slice(0, maxChars)}\n…[truncated]` : report;
}

function formatPreviewAuditReport(audit: PreviewAuditResult, maxChars = 4000) {
  const qualitySection = audit.quality_checks && audit.quality_checks.length > 0
    ? `quality_checks:\n- ${audit.quality_checks.map((check) => `${check.ok ? "ok" : "warn"}: ${check.label} - ${check.detail}`).join("\n- ")}`
    : null;
  const browserDetails = [
    audit.viewport?.width && audit.viewport?.height ? `desktop_viewport: ${audit.viewport.width}x${audit.viewport.height}` : null,
    audit.mobile_viewport?.width && audit.mobile_viewport?.height ? `mobile_viewport: ${audit.mobile_viewport.width}x${audit.mobile_viewport.height}` : null,
    typeof audit.interactive_count === "number" ? `interactive_count: ${audit.interactive_count}` : null,
    audit.unlabeled_interactive?.length ? `unlabeled_interactive:\n- ${audit.unlabeled_interactive.join("\n- ")}` : null,
    audit.mobile_text_overflow_nodes?.length ? `mobile_text_overflow:\n- ${audit.mobile_text_overflow_nodes.join("\n- ")}` : null,
    audit.small_tap_targets?.length ? `small_tap_targets:\n- ${audit.small_tap_targets.join("\n- ")}` : null,
    audit.broken_images?.length ? `broken_images:\n- ${audit.broken_images.join("\n- ")}` : null,
    audit.mobile_fixed_overlays?.length ? `mobile_fixed_overlays:\n- ${audit.mobile_fixed_overlays.join("\n- ")}` : null,
  ].filter(Boolean).join("\n");

  const sections = [
    `summary: ${audit.summary}`,
    `audit_mode: ${audit.audit_mode}`,
    browserDetails || null,
    audit.title ? `title: ${audit.title}` : "title: (missing)",
    audit.meta_description ? `meta: ${audit.meta_description}` : "meta: (missing)",
    audit.headings.length > 0 ? `headings: ${audit.headings.join(" | ")}` : "headings: (missing)",
    audit.buttons.length > 0 ? `buttons: ${audit.buttons.join(" | ")}` : "buttons: (none)",
    qualitySection,
    audit.runtime_warnings.length > 0 ? `runtime_warnings:\n- ${audit.runtime_warnings.join("\n- ")}` : null,
    audit.page_errors.length > 0 ? `page_errors:\n- ${audit.page_errors.join("\n- ")}` : null,
    audit.console_errors.length > 0 ? `console_errors:\n- ${audit.console_errors.join("\n- ")}` : null,
    audit.issues.length > 0 ? `issues:\n- ${audit.issues.join("\n- ")}` : "issues: none",
  ].filter(Boolean);

  if (audit.excerpt.trim()) {
    sections.push(`excerpt:\n${audit.excerpt.trim()}`);
  }

  const report = sections.join("\n\n");
  return report.length > maxChars ? `${report.slice(0, maxChars)}\n…[truncated]` : report;
}

function toAuditSnapshot(label: string, trace: AgentRunTrace, makeId: () => string): AgentAuditSnapshot {
  return {
    id: makeId(),
    label,
    passes: trace.passes,
    memoryHits: trace.memory_hits.map((hit) => ({
      kind: hit.kind,
      source: hit.source,
      title: hit.title,
      score: hit.score,
      text: hit.text,
    })),
    skills: trace.skills.map((skill) => ({
      skillId: skill.skill_id,
      title: skill.title,
      source: skill.source,
    })),
    mcpServers: trace.mcp_servers.map((server) => ({
      name: server.name,
      transport: server.transport,
      target: server.target,
      tools: server.tools,
      source: server.source,
    })),
    mcpToolsUsed: trace.mcp_tools_used.map((tool) => ({
      server: tool.server,
      tool: tool.tool,
      ok: tool.ok,
      durationMs: tool.duration_ms,
      error: tool.error,
      text: tool.text,
    })),
    plan: trace.plan || [],
    verification: trace.verification || [],
  };
}

function pushRunTrace(pushAgentLiveItem: WorkflowArgs["pushAgentLiveItem"], trace: AgentRunTrace | undefined) {
  if (!trace) return;

  const memoryCount = trace.memory_hits.length;
  const skillCount = trace.skills.length;
  const mcpUsedCount = trace.mcp_tools_used.length;
  const mcpSeenCount = trace.mcp_servers.length;
  const planCount = trace.plan?.length || 0;
  const verificationCount = trace.verification?.length || 0;

  pushAgentLiveItem({
    role: "tool",
    tone: "default",
    text: `Run trace: plan ${planCount}, verify ${verificationCount}, memory ${memoryCount}, skill ${skillCount}, MCP used ${mcpUsedCount}.`,
    meta: [`passes=${trace.passes}`, mcpSeenCount > 0 ? `mcp available=${mcpSeenCount}` : null].filter(Boolean).join(" • ") || null,
  });

  if (memoryCount > 0) {
    pushAgentLiveItem({
      role: "tool",
      tone: "default",
      text: "Memory yang kepake di run ini.",
      meta: trace.memory_hits.slice(0, 4).map((hit) => `${hit.title} (${hit.kind})`).join(" • "),
    });
  }

  if (skillCount > 0) {
    pushAgentLiveItem({
      role: "tool",
      tone: "default",
      text: "Skill yang dipilih agent buat ngerjain task ini.",
      meta: trace.skills.map((skill) => skill.skill_id).join(" • "),
    });
  }

  if (mcpUsedCount > 0) {
    pushAgentLiveItem({
      role: "tool",
      tone: trace.mcp_tools_used.every((tool) => tool.ok) ? "success" : "error",
      text: "Tool MCP yang beneran kepake di run ini.",
      meta: trace.mcp_tools_used.map((tool) => `${tool.server}.${tool.tool} (${tool.ok ? "ok" : "error"})`).join(" • "),
    });
  }

  if (trace.warnings && trace.warnings.length > 0) {
    pushAgentLiveItem({
      role: "tool",
      tone: "error",
      text: "Agent run ini punya warning boundary/fallback yang perlu kamu tahu.",
      meta: trace.warnings.slice(0, 3).map((warning) => `${warning.phase}: ${warning.message}`).join(" • "),
    });
  }
}

function failedVerifierChecks(trace: AgentRunTrace | undefined) {
  return (trace?.verification || []).filter((check) => !check.ok);
}

function blockingVerifierChecks(trace: AgentRunTrace | undefined) {
  return failedVerifierChecks(trace).filter((check) => check.name !== "full-agent-coverage");
}

function verifierFailureSummary(trace: AgentRunTrace | undefined) {
  const failed = failedVerifierChecks(trace);
  if (failed.length === 0) return "";
  return failed.slice(0, 3).map((check) => `${check.name}: ${check.detail}`).join(" • ");
}

function retryActionsForFailedShell(results: ShellActionRun[]): AgentAction[] {
  const seen = new Set<string>();
  return results
    .filter((result) => !result.ok && result.command.trim())
    .map((result) => result.command.trim())
    .filter((command) => {
      if (seen.has(command)) return false;
      seen.add(command);
      return true;
    })
    .map((command) => ({ type: "shell", command }));
}

export async function runAgentWorkflow({
  agentInput,
  agentStatus,
  buildMode,
  friendlyFreeTierMode,
  previewUrl,
  selectedProject,
  attachedImagePath,
  activeFile,
  openFiles,
  buffers,
  makeAgentLiveId,
  pushAgentLiveItem,
  appendAssistantLiveText,
  refreshExplorer,
  ensurePreviewRunning,
  refreshPreviewFrame,
  notify,
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
}: WorkflowArgs) {
  if (!agentInput.trim() || agentStatus === "thinking") return;

  const runPlan = getAgentRunPlan(buildMode, agentInput, previewUrl);
  const { requestEditorStatus, shouldDrivePreview, shouldRunValidation, shouldAuditPreview, intent: inputIntent } = runPlan;

  let workingBuffers = buffers;
  let currentPreviewUrl = previewUrl || "";
  let combinedActions: AgentAction[] = [];
  let finalStatus = "Agent task finished";
  let finalToast: WorkflowToast = {
    kind: "success",
    message: "Tugas selesai",
  };

  const intentSummary = (intent: Pick<AgentIntent, "kind" | "confidence" | "rationale"> | { kind: string; confidence: number; rationale: string }) => {
    const label = intent.kind === "conversation"
      ? "percakapan"
      : intent.kind === "inspection"
        ? "audit baca-saja"
        : intent.kind === "mixed"
          ? "campuran"
          : "perintah build";
    return `${label} (${Math.round(intent.confidence * 100)}%)`;
  };

  const syncBuffers = (changes: AgentChange[]) => {
    workingBuffers = mergeBuffersWithChanges(workingBuffers, changes);
    setBuffers(workingBuffers);
  };

  const appendLogSection = (label: string, content: string) => {
    if (!content.trim()) return;
    setAgentLog((prev) => `${prev}\n\n[${label}]\n${content}`);
  };

  const runAgentPass = async (prompt: string, passEditorStatus: string, resetReply = true) => {
    const seenPhases = new Set<string>();
    if (resetReply) setAgentReply("");

    return streamAgent(
      prompt,
      (event) => {
        if (event.event === "status") {
          const phase = typeof event.data.phase === "string" ? event.data.phase : "";
          const message = typeof event.data.message === "string" ? event.data.message : "";
          const jobId = typeof event.data.job_id === "string" ? event.data.job_id : "";
          setWorkingMsg(message || PHASE_LABELS[phase] || "Agent lagi kerja…");
          if (phase) setEditorStatus(PHASE_LABELS[phase] || passEditorStatus);
          if (phase && !seenPhases.has(phase)) {
            seenPhases.add(phase);
            if (phase === "queued" && jobId) {
              pushAgentLiveItem({
                role: "tool",
                tone: "working",
                text: "Durable agent job dibuat.",
                meta: `job ${jobId.slice(0, 8)}`,
              });
            }
          }
          return;
        }

        if (event.event === "delta") {
          const spokenChunk = typeof event.data.spoken_chunk === "string" ? event.data.spoken_chunk : "";
          const message = typeof event.data.message === "string" ? event.data.message : "";
          if (spokenChunk) {
            setAgentReply((prev) => (prev ? `${prev} ${spokenChunk}` : spokenChunk));
            appendAssistantLiveText(spokenChunk);
          }
          if (message) setWorkingMsg(message);
        }
      },
      activeFile || null,
      null,
      selectedProject,
      buildMode,
      attachedImagePath ? [attachedImagePath] : undefined,
      activeFile ? (workingBuffers[activeFile]?.content ?? null) : null,
      openFiles,
      currentPreviewUrl || null,
      passEditorStatus,
    );
  };

  const applyChanges = async (changes: AgentChange[], statusLabel = "Applying") => {
    if (changes.length === 0) return;
    setWorkingMsg(`Menerapkan ${changes.length} perubahan…`);
    setEditorStatus(`${statusLabel} ${changes.length} file changes...`);
    const checkpointFiles = await Promise.all(changes.map(async (change) => {
      const openBuffer = workingBuffers[change.path]?.content;
      if (typeof openBuffer === "string") {
        return { path: change.path, previous_content: openBuffer };
      }
      try {
        const current = await readFile(change.path);
        return { path: change.path, previous_content: current.content };
      } catch {
        return { path: change.path, previous_content: null };
      }
    }));
    const checkpoint = {
      created_at: new Date().toISOString(),
      project_root: selectedProject,
      apply_mode: "patch",
      files: checkpointFiles.map((file) => {
        const change = changes.find((item) => item.path === file.path);
        return {
          ...file,
          patch: change?.diff || "",
          old_sha256: change?.old_sha256 || null,
          new_sha256: change?.new_sha256 || null,
          old_exists: typeof change?.old_exists === "boolean" ? change.old_exists : file.previous_content !== null,
        };
      }),
    };
    const checkpointPath = checkpointPathForRun(selectedProject);
    pushAgentLiveItem({
      role: "tool",
      tone: "success",
      text: `Aku terapin ${changes.length} file ke project dulu.`,
      meta: `checkpoint ${checkpointPath}`,
    });
    await applyMany([
      { path: checkpointPath, content: JSON.stringify(checkpoint, null, 2) + "\n" },
      ...changes.map((change) => ({
        path: change.path,
        content: change.new_content,
        expected_sha256: change.old_sha256 || null,
        expected_exists: typeof change.old_exists === "boolean" ? change.old_exists : null,
      })),
    ], true);
    syncBuffers(changes);
    await refreshExplorer();
  };

  const runShellActionsForPass = async (actions: AgentAction[]) => {
    const results: ShellActionRun[] = [];

    for (const action of actions) {
      const actionType = String(action.type || "").trim().toLowerCase();
      if (action.type === "mcp") {
        pushAgentLiveItem({
          role: "tool",
          tone: "error",
          text: "Ada action MCP mentah yang lolos ke frontend. Harusnya ini udah diberesin di backend agent loop.",
          meta: JSON.stringify(action),
        });
        continue;
      }
      if (action.type === "tool") {
        pushAgentLiveItem({
          role: "tool",
          tone: "error",
          text: "Ada action tool mentah yang lolos ke frontend. Tool read-only harus dieksekusi backend sebelum final.",
          meta: JSON.stringify(action),
        });
        continue;
      }
      if (actionType !== "shell" || typeof action.command !== "string") {
        pushAgentLiveItem({
          role: "tool",
          tone: "error",
          text: `Action agent tidak dikenal: ${actionType || "unknown"}.`,
          meta: JSON.stringify(action),
        });
        continue;
      }
      setWorkingMsg(`Menjalankan: ${action.command}`);
      pushAgentLiveItem({ role: "tool", tone: "working", text: "Aku jalanin command tambahan buat ngeberesin flow.", meta: action.command });
      try {
        const runRes = await terminalRun(action.command, selectedProject !== "." ? selectedProject : undefined);
        results.push({ command: action.command, ...runRes });
        appendLogSection(runRes.ok ? "TERMINAL STDOUT" : "TERMINAL STDERR", runRes.ok ? runRes.stdout : runRes.stderr);
        pushAgentLiveItem({
          role: "tool",
          tone: runRes.ok ? "success" : "error",
          text: runRes.ok ? "Command-nya selesai tanpa masalah." : "Command-nya jalan, tapi keluar sinyal error yang perlu dicek.",
          meta: action.command,
        });
        if (typeof runRes.synced_files === "number" && runRes.synced_files > 0) {
          await refreshExplorer();
        }
      } catch (error) {
        const message = errorMessage(error);
        results.push({
          command: action.command,
          ok: false,
          stdout: "",
          stderr: "",
          returncode: 1,
          error: message,
        });
        appendLogSection("TERMINAL ERROR", message);
        pushAgentLiveItem({ role: "tool", tone: "error", text: "Ada command yang gagal dieksekusi.", meta: message });
      }
    }

    return results;
  };

  const refreshPreviewSurface = async () => {
    if (!shouldDrivePreview) return currentPreviewUrl;
    setWorkingMsg(currentPreviewUrl ? "Menyegarkan preview…" : "Menyalakan preview…");
    pushAgentLiveItem({
      role: "tool",
      tone: "working",
      text: currentPreviewUrl ? "Aku refresh preview biar hasil terbaru langsung kelihatan." : "Aku nyalain preview dulu biar hasilnya bisa dilihat hidup.",
    });

    if (currentPreviewUrl) {
      refreshPreviewFrame();
      setEditorStatus("Preview refreshed after agent changes");
      return currentPreviewUrl;
    }

    currentPreviewUrl = (await ensurePreviewRunning()) || "";
    return currentPreviewUrl;
  };

  const runValidationPass = async (label: string) => {
    setWorkingMsg("Menjalankan validasi proyek…");
    const validation = await validateProject(selectedProject);
    appendLogSection(label, formatValidationReport(validation));
    pushAgentLiveItem({
      role: "tool",
      tone: validation.ok ? "success" : "error",
      text: validation.ok ? "Validasinya lolos." : "Validasinya nemu problem yang harus kubenerin lagi.",
      meta: label,
    });
    return validation;
  };

  const runPreviewAuditPass = async (url: string, label: string) => {
    if (!url) return null;
    setWorkingMsg("Mengaudit preview yang lagi live…");
    const audit = await auditPreview(url, selectedProject);
    appendLogSection(label, formatPreviewAuditReport(audit));
    pushAgentLiveItem({
      role: "tool",
      tone: audit.issues.length === 0 ? "success" : "error",
      text: audit.issues.length === 0 ? "Preview-nya aman, nggak ada temuan visual penting." : `Preview audit nemu ${audit.issues.length} hal yang masih perlu dirapihin.`,
      meta: label,
    });
    return audit;
  };

  setAgentStatus("thinking");
  setAgentWidgetOpen(true);
  setAgentRunViewPinned(true);
  setAgentReply("");
  setAgentLog("");
  setAgentActions([]);
  setAgentAuditTrail([]);
  setAgentLiveItems([{ id: makeAgentLiveId(), role: "user", tone: "default", text: agentInput.trim() }]);
  setEditorStatus(requestEditorStatus);
  setWorkingMsg("Agent sedang berpikir…");
  pushAgentLiveItem({
    role: "tool",
    tone: inputIntent.kind === "command" || inputIntent.kind === "mixed" ? "working" : "default",
    text: `Intent kebaca sebagai ${intentSummary(inputIntent)}.`,
    meta: inputIntent.rationale,
  });
  if (friendlyFreeTierMode) {
    pushAgentLiveItem({
      role: "tool",
      tone: "default",
      text: "Free-tier guard aktif: agent hemat panggilan model dan context supaya limit provider nggak cepat mentok.",
      meta: "1 call untuk build normal, repair hanya kalau validasi gagal",
    });
  }

  void fetchAgentCapabilities(selectedProject, false)
    .then((caps) => {
      const mcpCount = caps.discovered_mcp_servers.length;
      const memoryParts = [];
      if (caps.memory.session_entries > 0) memoryParts.push(`${caps.memory.session_entries} memori session`);
      if (caps.memory.project_entries > 0) memoryParts.push(`${caps.memory.project_entries} memori project`);
      if (caps.memory.has_project_profile) memoryParts.push("project profile aktif");
      const memoryLabel = memoryParts.length > 0 ? memoryParts.join(" + ") : "memory masih fresh";
      const memoryBackendLabel = caps.memory.retrieval_backend ? `rag: ${caps.memory.retrieval_backend}` : null;
      const memoryWarningLabel = caps.memory.supabase_warning || null;
      const mcpLabel = mcpCount > 0
        ? `${mcpCount} MCP server siap dipakai`
        : "belum ada MCP server yang dikonfigurasi";
      const stackBits = [];
      if (caps.stack.component_libraries.length > 0) stackBits.push(`ui libs: ${caps.stack.component_libraries.join(", ")}`);
      if (caps.stack.playwright) stackBits.push("playwright terdeteksi");
      else if (caps.stack.headless_browser) stackBits.push("headless browser terdeteksi");
      if (caps.stack.node_runtime) stackBits.push(`preview audit: ${caps.stack.preview_audit_mode}`);
      else stackBits.push("node runtime belum ada untuk browser audit");
      if (caps.stack.webcontainer) stackBits.push("webcontainer package terdeteksi");
      pushAgentLiveItem({
        role: "tool",
        tone: "default",
        text: `Capability check: ${memoryLabel}, ${mcpLabel}.`,
        meta: [
          caps.supports.autonomous_mcp_loop ? "autonomous tool loop aktif" : null,
          caps.supports.command_conversation_boundary ? "command/conversation boundary aktif" : null,
          caps.supports.read_only_inspection_boundary ? "inspection boundary aktif" : null,
          caps.supports.supabase_rag_ready
            ? "supabase memory backend siap"
            : caps.supports.supabase_memory_backend
              ? "supabase memory backend terpasang tapi belum siap"
              : null,
          caps.supports.vector_memory_retrieval ? "vector retrieval aktif" : null,
          caps.supports.preview_quality_checks ? "quality audit responsive+a11y+states aktif" : null,
          caps.supports.provider_fallback_routing ? "provider fallback aktif" : null,
          memoryBackendLabel,
          memoryWarningLabel,
          ...stackBits,
        ].filter(Boolean).join(" • ") || null,
      });
    })
    .catch(() => {
      // capability preflight is helpful, but should never block the run
    });

  try {
    const res = await runAgentPass(agentInput, requestEditorStatus);
    setAgentReply(res.spoken);
    setAgentLog(res.log);

    let changes: AgentChange[] = res.changes || [];
    let actions: AgentAction[] = res.actions || [];
    const resolvedIntent = res.intent || inputIntent;

    pushAgentLiveItem({
      role: "tool",
      tone: resolvedIntent.kind === "command" || resolvedIntent.kind === "mixed" ? "success" : "default",
      text: `Backend intent final: ${intentSummary(resolvedIntent)}.`,
      meta: resolvedIntent.rationale,
    });
    let mainTrace = res.trace;
    pushRunTrace(pushAgentLiveItem, mainTrace);
    if (mainTrace) {
      setAgentAuditTrail([toAuditSnapshot("Main pass", mainTrace, makeAgentLiveId)]);
    }
    let mainVerifierFailures = failedVerifierChecks(mainTrace);
    let mainBlockingVerifierFailures = blockingVerifierChecks(mainTrace);
    if (mainVerifierFailures.length > 0) {
      pushAgentLiveItem({
        role: "tool",
        tone: "error",
        text: `Verifier nemu ${mainVerifierFailures.length} masalah di output agent.`,
        meta: verifierFailureSummary(mainTrace),
      });
    }

    if (
      mainBlockingVerifierFailures.length > 0
      && (resolvedIntent.kind === "command" || resolvedIntent.kind === "mixed")
    ) {
      setWorkingMsg("Verifier gagal, agent memperbaiki output sebelum apply…");
      setEditorStatus("Repairing verifier issues before apply...");
      const verifierRepairRes = await runAgentPass(
        buildVerifierRepairPrompt(buildMode, agentInput, verifierFailureSummary(mainTrace)),
        "Repairing verifier issues before apply...",
      );
      appendLogSection("VERIFIER REPAIR PASS", verifierRepairRes.log);
      if (verifierRepairRes.spoken) setAgentReply(verifierRepairRes.spoken);
      const verifierRepairTrace = verifierRepairRes.trace;
      pushRunTrace(pushAgentLiveItem, verifierRepairTrace);
      if (verifierRepairTrace) {
        setAgentAuditTrail((prev) => [...prev, toAuditSnapshot("Verifier repair pass", verifierRepairTrace, makeAgentLiveId)]);
      }
      changes = verifierRepairRes.changes || [];
      actions = verifierRepairRes.actions || [];
      mainTrace = verifierRepairTrace;
      mainVerifierFailures = failedVerifierChecks(mainTrace);
      mainBlockingVerifierFailures = blockingVerifierChecks(mainTrace);
      if (mainVerifierFailures.length > 0) {
        pushAgentLiveItem({
          role: "tool",
          tone: "error",
          text: `Verifier repair masih nemu ${mainVerifierFailures.length} masalah.`,
          meta: verifierFailureSummary(mainTrace),
        });
      }
    }

    combinedActions = [...actions];
    setAgentActions(combinedActions);

    const outputSafeToApply = mainBlockingVerifierFailures.length === 0;
    if (!outputSafeToApply) {
      pushAgentLiveItem({
        role: "tool",
        tone: "error",
        text: "Output agent diblokir sebelum apply karena gagal verifier.",
        meta: verifierFailureSummary(mainTrace),
      });
    }

    if (outputSafeToApply && changes.length > 0) {
      await applyChanges(changes);
    }

    const shellResults = outputSafeToApply && actions.length > 0
      ? await runShellActionsForPass(actions)
      : [];

    const auditedPreviewUrl = outputSafeToApply && changes.length > 0 ? await refreshPreviewSurface() : currentPreviewUrl;

    let validation: ProjectValidationRun | null = null;
    let previewAudit: PreviewAuditResult | null = null;

    if ((resolvedIntent.kind === "conversation" || resolvedIntent.kind === "inspection") && changes.length === 0 && actions.length === 0) {
      finalStatus = resolvedIntent.kind === "inspection"
        ? "Inspection answered without file changes"
        : "Conversation answered without file changes";
      finalToast = { kind: "success", message: "Jawaban siap, tidak ada perubahan file" };
    }

    if (outputSafeToApply && (changes.length > 0 || actions.length > 0) && shouldRunValidation) {
      validation = await runValidationPass("VALIDATION PASS 1");
    }
    if (outputSafeToApply && auditedPreviewUrl && (changes.length > 0 || actions.length > 0) && shouldAuditPreview) {
      previewAudit = await runPreviewAuditPass(auditedPreviewUrl, "PREVIEW AUDIT 1");
    }

    let latestValidation = validation;
    let latestPreviewAudit = previewAudit;
    let latestShellResults = shellResults;
    let latestVerifierFailures = mainVerifierFailures;
    const maxRepairPasses = friendlyFreeTierMode ? 1 : 2;
    const needsRepair = () => {
      const validationFailing = Boolean(latestValidation && !latestValidation.ok);
      const shellFailing = latestShellResults.some((result) => !result.ok);
      const previewFailing = Boolean(latestPreviewAudit && latestPreviewAudit.issues.length > 0);
      return shellFailing || validationFailing || (!friendlyFreeTierMode && previewFailing);
    };
    const hasValidationIssues = Boolean(latestValidation && !latestValidation.ok);
    const hasPreviewIssues = Boolean(latestPreviewAudit && latestPreviewAudit.issues.length > 0);

    if ((resolvedIntent.kind === "conversation" || resolvedIntent.kind === "inspection") && changes.length === 0 && actions.length === 0) {
      // pure read-only run, nothing else to do
    } else if (needsRepair() && (changes.length > 0 || actions.length > 0)) {
      let repairedPreviewUrl = auditedPreviewUrl;

      for (let pass = 1; pass <= maxRepairPasses && needsRepair(); pass += 1) {
        setWorkingMsg(`Memperbaiki hasil audit, putaran ${pass}/${maxRepairPasses}…`);
        setEditorStatus(`Fixing preview and validation issues (${pass}/${maxRepairPasses})...`);
        pushAgentLiveItem({
          role: "tool",
          tone: "working",
          text: `Repair loop ${pass}/${maxRepairPasses}: agent baca output terbaru lalu coba benerin lagi.`,
          meta: friendlyFreeTierMode ? "free-tier guard: maksimal 1 repair" : "bounded loop: maksimal 2 repair",
        });

        const validationReport = latestValidation && !latestValidation.ok ? formatValidationReport(latestValidation, 6000) : null;
        const shellReport = latestShellResults.some((result) => !result.ok) ? formatShellActionReport(latestShellResults, 6000) : null;
        const previewAuditReport = latestPreviewAudit && latestPreviewAudit.issues.length > 0 ? formatPreviewAuditReport(latestPreviewAudit, 3500) : null;

        const repairRes = await runAgentPass(
          buildRepairPrompt(buildMode, agentInput, validationReport, previewAuditReport, shellReport, pass, maxRepairPasses),
          `Fixing preview and validation issues (${pass}/${maxRepairPasses})...`,
        );

        appendLogSection(`REPAIR PASS ${pass}`, repairRes.log);
        if (repairRes.spoken) setAgentReply(repairRes.spoken);
        const repairTrace = repairRes.trace;
        pushRunTrace(pushAgentLiveItem, repairTrace);
        if (repairTrace) {
          setAgentAuditTrail((prev) => [...prev, toAuditSnapshot(`Repair pass ${pass}`, repairTrace, makeAgentLiveId)]);
        }
        latestVerifierFailures = failedVerifierChecks(repairTrace);
        if (latestVerifierFailures.length > 0) {
          pushAgentLiveItem({
            role: "tool",
            tone: "error",
            text: `Verifier repair masih nemu ${latestVerifierFailures.length} masalah.`,
            meta: verifierFailureSummary(repairTrace),
          });
        }

        const repairChanges: AgentChange[] = repairRes.changes || [];
        const repairActions: AgentAction[] = repairRes.actions || [];
        combinedActions = [...combinedActions, ...repairActions];
        setAgentActions(combinedActions);

        if (repairChanges.length === 0 && repairActions.length === 0) {
          pushAgentLiveItem({
            role: "tool",
            tone: "error",
            text: "Repair pass tidak menghasilkan perubahan atau command baru.",
            meta: "Loop dihentikan supaya tidak buang limit.",
          });
          break;
        }

        const retryFailedShellActions = retryActionsForFailedShell(latestShellResults);

        if (repairChanges.length > 0) {
          await applyChanges(repairChanges, "Applying repair");
        }
        const shellActionsToRun = repairActions.length > 0
          ? repairActions
          : repairChanges.length > 0
            ? retryFailedShellActions
            : [];
        if (repairActions.length === 0 && shellActionsToRun.length > 0) {
          pushAgentLiveItem({
            role: "tool",
            tone: "working",
            text: "Repair sudah diterapkan, sekarang rerun command yang sebelumnya gagal.",
            meta: shellActionsToRun.map((action) => String(action.command || "")).join(" • "),
          });
        }
        latestShellResults = shellActionsToRun.length > 0
          ? await runShellActionsForPass(shellActionsToRun)
          : [];

        repairedPreviewUrl = repairChanges.length > 0 ? await refreshPreviewSurface() : repairedPreviewUrl;
        latestValidation = shouldRunValidation ? await runValidationPass(`VALIDATION PASS ${pass + 1}`) : latestValidation;
        latestPreviewAudit = repairedPreviewUrl && shouldAuditPreview ? await runPreviewAuditPass(repairedPreviewUrl, `PREVIEW AUDIT ${pass + 1}`) : latestPreviewAudit;
      }

      const validationStillFailing = Boolean(latestValidation && !latestValidation.ok);
      const shellStillFailing = latestShellResults.some((result) => !result.ok);
      const previewStillFailing = Boolean(latestPreviewAudit && latestPreviewAudit.issues.length > 0);
      const verifierStillFailing = latestVerifierFailures.length > 0;

      if (!shellStillFailing && !validationStillFailing && !previewStillFailing && !verifierStillFailing) {
        finalStatus = "Agent task finished, validated, and preview-audited";
        finalToast = { kind: "success", message: "Tugas selesai, lolos validasi, dan preview lebih rapi" };
      } else {
        finalStatus = verifierStillFailing
          ? "Agent verifier still found issues"
          : shellStillFailing
            ? "Agent shell command still failing"
            : previewStillFailing
              ? "Preview audit still found issues"
              : "Validation still failing";
        finalToast = { kind: "warning", message: "Perubahan diterapkan, tapi masih ada temuan command/verifier/audit yang perlu dicek" };
      }
    } else if (!hasValidationIssues && !hasPreviewIssues && (validation || previewAudit)) {
      finalStatus = previewAudit ? "Agent task finished, validated, and preview-audited" : "Agent task finished and validated";
      finalToast = {
        kind: "success",
        message: previewAudit ? "Tugas selesai, lolos validasi, dan preview diaudit" : "Tugas selesai dan lolos validasi",
      };
    } else if (mainVerifierFailures.length > 0) {
      finalStatus = "Agent verifier found issues";
      finalToast = {
        kind: outputSafeToApply && (changes.length > 0 || actions.length > 0) ? "warning" : "error",
        message: outputSafeToApply && (changes.length > 0 || actions.length > 0)
          ? "Agent selesai dengan warning verifier"
          : "Output agent diblokir karena gagal verifier",
      };
    } else if (changes.length > 0 && shouldDrivePreview) {
      finalStatus = auditedPreviewUrl ? "Preview refreshed after agent changes" : "Preview live after agent changes";
    }

    setEditorStatus(finalStatus);
    if (finalToast.kind === "success") {
      notify(finalToast);
    } else if (finalToast.kind === "warning") {
      notify(finalToast);
    } else {
      notify(finalToast);
    }
    setAgentStatus("idle");
  } catch (error) {
    setEditorStatus("Agent failed");
    setAgentStatus("error");
    notify({ kind: "error", message: `Agent error: ${errorMessage(error)}` });
  } finally {
    setWorkingMsg("");
  }
}
