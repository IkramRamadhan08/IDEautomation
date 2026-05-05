import type { Dispatch, SetStateAction } from "react";
import {
  applyMany,
  auditPreview,
  fetchAgentCapabilities,
  streamAgent,
  terminalRun,
  validateProject,
  type AgentIntent,
  type AgentRunTrace,
  type PreviewAuditResult,
  type ProjectValidationRun,
} from "../api";
import type { AgentAction, AgentAuditSnapshot, AgentChange, AgentLiveItem, BuildMode, FileBuffer } from "../types";
import { buildRepairPrompt, getAgentRunPlan } from "./runtime";

type Setter<T> = Dispatch<SetStateAction<T>>;

type ToastKind = "success" | "warning" | "error";

type AgentStatus = "idle" | "thinking" | "error";

type WorkflowToast = {
  kind: ToastKind;
  message: string;
};

type WorkflowArgs = {
  agentInput: string;
  agentStatus: AgentStatus;
  buildMode: BuildMode;
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
  context_ready: "Konteks siap, mulai ngerjain…",
  drafting: "Lagi nulis solusi pertamanya…",
  tooling: "Lagi jalanin tool agent…",
  refining: "Lagi merapikan hasil…",
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
    `audit_mode: ${audit.audit_mode}`,
    audit.title ? `title: ${audit.title}` : "title: (missing)",
    audit.meta_description ? `meta: ${audit.meta_description}` : "meta: (missing)",
    audit.headings.length > 0 ? `headings: ${audit.headings.join(" | ")}` : "headings: (missing)",
    audit.buttons.length > 0 ? `buttons: ${audit.buttons.join(" | ")}` : "buttons: (none)",
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
  };
}

function pushRunTrace(pushAgentLiveItem: WorkflowArgs["pushAgentLiveItem"], trace: AgentRunTrace | undefined) {
  if (!trace) return;

  const memoryCount = trace.memory_hits.length;
  const skillCount = trace.skills.length;
  const mcpUsedCount = trace.mcp_tools_used.length;
  const mcpSeenCount = trace.mcp_servers.length;

  pushAgentLiveItem({
    role: "tool",
    tone: "default",
    text: `Run trace: memory ${memoryCount}, skill ${skillCount}, MCP used ${mcpUsedCount}.`,
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

export async function runAgentWorkflow({
  agentInput,
  agentStatus,
  buildMode,
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
          setWorkingMsg(message || PHASE_LABELS[phase] || "Agent lagi kerja…");
          if (phase) setEditorStatus(PHASE_LABELS[phase] || passEditorStatus);
          if (phase && !seenPhases.has(phase)) {
            seenPhases.add(phase);
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
    pushAgentLiveItem({
      role: "tool",
      tone: "success",
      text: `Aku terapin ${changes.length} file ke project dulu.`,
      meta: changes.slice(0, 3).map((change) => change.path).join(" • ") || null,
    });
    await applyMany(changes.map((change) => ({ path: change.path, content: change.new_content })), true);
    syncBuffers(changes);
    await refreshExplorer();
  };

  const runShellActionsForPass = async (actions: AgentAction[]) => {
    for (const action of actions) {
      if (action.type === "mcp") {
        pushAgentLiveItem({
          role: "tool",
          tone: "error",
          text: "Ada action MCP mentah yang lolos ke frontend. Harusnya ini udah diberesin di backend agent loop.",
          meta: JSON.stringify(action),
        });
        continue;
      }
      if (action.type !== "shell" || typeof action.command !== "string") continue;
      setWorkingMsg(`Menjalankan: ${action.command}`);
      pushAgentLiveItem({ role: "tool", tone: "working", text: "Aku jalanin command tambahan buat ngeberesin flow.", meta: action.command });
      try {
        const runRes = await terminalRun(action.command, selectedProject !== "." ? selectedProject : undefined);
        appendLogSection(runRes.ok ? "TERMINAL STDOUT" : "TERMINAL STDERR", runRes.ok ? runRes.stdout : runRes.stderr);
        pushAgentLiveItem({
          role: "tool",
          tone: runRes.ok ? "success" : "error",
          text: runRes.ok ? "Command-nya selesai tanpa masalah." : "Command-nya jalan, tapi keluar sinyal error yang perlu dicek.",
          meta: action.command,
        });
      } catch (error) {
        appendLogSection("TERMINAL ERROR", errorMessage(error));
        pushAgentLiveItem({ role: "tool", tone: "error", text: "Ada command yang gagal dieksekusi.", meta: errorMessage(error) });
      }
    }
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

  void fetchAgentCapabilities(selectedProject, false)
    .then((caps) => {
      const mcpCount = caps.discovered_mcp_servers.length;
      const memoryParts = [];
      if (caps.memory.session_entries > 0) memoryParts.push(`${caps.memory.session_entries} memori session`);
      if (caps.memory.project_entries > 0) memoryParts.push(`${caps.memory.project_entries} memori project`);
      const memoryLabel = memoryParts.length > 0 ? memoryParts.join(" + ") : "memory masih fresh";
      const memoryBackendLabel = caps.memory.retrieval_backend ? `rag: ${caps.memory.retrieval_backend}` : null;
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
          caps.supports.supabase_memory_backend ? "supabase memory backend siap" : null,
          memoryBackendLabel,
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

    const changes: AgentChange[] = res.changes || [];
    const actions: AgentAction[] = res.actions || [];
    const resolvedIntent = res.intent || inputIntent;

    pushAgentLiveItem({
      role: "tool",
      tone: resolvedIntent.kind === "command" || resolvedIntent.kind === "mixed" ? "success" : "default",
      text: `Backend intent final: ${intentSummary(resolvedIntent)}.`,
      meta: resolvedIntent.rationale,
    });
    const mainTrace = res.trace;
    pushRunTrace(pushAgentLiveItem, mainTrace);
    if (mainTrace) {
      setAgentAuditTrail([toAuditSnapshot("Main pass", mainTrace, makeAgentLiveId)]);
    }

    combinedActions = [...actions];
    setAgentActions(combinedActions);

    if (changes.length > 0) {
      await applyChanges(changes);
    }

    if (actions.length > 0) {
      await runShellActionsForPass(actions);
    }

    const auditedPreviewUrl = changes.length > 0 ? await refreshPreviewSurface() : currentPreviewUrl;

    let validation: ProjectValidationRun | null = null;
    let previewAudit: PreviewAuditResult | null = null;

    if ((resolvedIntent.kind === "conversation" || resolvedIntent.kind === "inspection") && changes.length === 0 && actions.length === 0) {
      finalStatus = resolvedIntent.kind === "inspection"
        ? "Inspection answered without file changes"
        : "Conversation answered without file changes";
      finalToast = { kind: "success", message: "Jawaban siap, tidak ada perubahan file" };
    }

    if ((changes.length > 0 || actions.length > 0) && shouldRunValidation) {
      validation = await runValidationPass("VALIDATION PASS 1");
    }
    if (auditedPreviewUrl && (changes.length > 0 || actions.length > 0) && shouldAuditPreview) {
      previewAudit = await runPreviewAuditPass(auditedPreviewUrl, "PREVIEW AUDIT 1");
    }

    const hasValidationIssues = Boolean(validation && !validation.ok);
    const hasPreviewIssues = Boolean(previewAudit && previewAudit.issues.length > 0);

    if ((resolvedIntent.kind === "conversation" || resolvedIntent.kind === "inspection") && changes.length === 0 && actions.length === 0) {
      // pure read-only run, nothing else to do
    } else if ((hasValidationIssues || hasPreviewIssues) && changes.length > 0) {
      setWorkingMsg("Memperbaiki hasil audit…");
      setEditorStatus("Fixing preview and validation issues...");

      const validationReport = validation && !validation.ok ? formatValidationReport(validation, 6000) : null;
      const previewAuditReport = previewAudit && previewAudit.issues.length > 0 ? formatPreviewAuditReport(previewAudit, 3500) : null;

      const repairRes = await runAgentPass(
        buildRepairPrompt(buildMode, agentInput, validationReport, previewAuditReport),
        "Fixing preview and validation issues...",
      );

      appendLogSection("REPAIR PASS", repairRes.log);
      if (repairRes.spoken) setAgentReply(repairRes.spoken);
      const repairTrace = repairRes.trace;
      pushRunTrace(pushAgentLiveItem, repairTrace);
      if (repairTrace) {
        setAgentAuditTrail((prev) => [...prev, toAuditSnapshot("Repair pass", repairTrace, makeAgentLiveId)]);
      }

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
