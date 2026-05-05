import type { Dispatch, SetStateAction } from "react";
import {
  applyMany,
  auditPreview,
  fetchAgentCapabilities,
  streamAgent,
  terminalRun,
  validateProject,
  type PreviewAuditResult,
  type ProjectValidationRun,
} from "../api";
import type { AgentAction, AgentChange, AgentLiveItem, BuildMode, FileBuffer } from "../types";
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
  setEditorStatus: Setter<string>;
  setWorkingMsg: Setter<string>;
};

const PHASE_LABELS: Record<string, string> = {
  queued: "Masuk antrean kerja…",
  starting: "Nyusun konteks kerja…",
  memory: "Ngambil memory yang relevan…",
  skills: "Milih skill yang paling kepake…",
  mcp: "Ngecek integrasi MCP yang tersedia…",
  context_ready: "Konteks siap, mulai ngerjain…",
  drafting: "Lagi nulis solusi pertamanya…",
  tooling: "Lagi jalanin tool agent…",
  refining: "Lagi merapikan hasil…",
  diffing: "Lagi nyusun patch yang rapi…",
};

const PHASE_SPEECH: Record<string, string> = {
  queued: "Oke, gue terima task-nya dulu.",
  starting: "Gue cek konteks project sama file yang lagi relevan.",
  memory: "Gue tarik dulu memori session sama memori project yang masih nyambung sama task ini.",
  skills: "Sekarang gue pilih skill kerja yang paling cocok buat ngerjain ini.",
  mcp: "Gue cek juga ada integrasi MCP apa aja yang bisa dipakai kalau butuh context tambahan.",
  context_ready: "Konteksnya udah kebaca, sekarang gue cari jalur yang paling masuk akal.",
  drafting: "Ketemu arah awalnya, gue mulai nulis perubahan.",
  tooling: "Ada info yang lebih aman diambil lewat tool dulu, jadi gue jalanin itu sebentar.",
  refining: "Gue rapihin dulu biar hasilnya nggak terasa asal jadi.",
  diffing: "Terakhir, gue susun patch-nya biar rapi dipasang ke project.",
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
  setEditorStatus,
  setWorkingMsg,
}: WorkflowArgs) {
  if (!agentInput.trim() || agentStatus === "thinking") return;

  const runPlan = getAgentRunPlan(buildMode, agentInput, previewUrl);
  const { requestEditorStatus, shouldDrivePreview, shouldRunValidation, shouldAuditPreview } = runPlan;

  let workingBuffers = buffers;
  let currentPreviewUrl = previewUrl || "";
  let combinedActions: AgentAction[] = [];
  let finalStatus = "Agent task finished";
  let finalToast: WorkflowToast = {
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
            pushAgentLiveItem({ role: "assistant", tone: "working", text: PHASE_SPEECH[phase] || message || "Gue lagi jalan." });
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
    pushAgentLiveItem({ role: "assistant", tone: "working", text: "Sekarang aku validasi dulu, biar nggak cuma kelihatan jadi tapi juga aman dijalanin." });
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
    pushAgentLiveItem({ role: "assistant", tone: "working", text: "Aku cek tampilan live-nya juga, bukan cuma source code-nya." });
    const audit = await auditPreview(url);
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
  setAgentLiveItems([{ id: makeAgentLiveId(), role: "user", tone: "default", text: agentInput.trim() }]);
  setEditorStatus(requestEditorStatus);
  setWorkingMsg("Agent sedang berpikir…");

  void fetchAgentCapabilities(selectedProject, false)
    .then((caps) => {
      const mcpCount = caps.discovered_mcp_servers.length;
      const memoryParts = [];
      if (caps.memory.session_entries > 0) memoryParts.push(`${caps.memory.session_entries} memori session`);
      if (caps.memory.project_entries > 0) memoryParts.push(`${caps.memory.project_entries} memori project`);
      const memoryLabel = memoryParts.length > 0 ? memoryParts.join(" + ") : "memory masih fresh";
      const mcpLabel = mcpCount > 0
        ? `${mcpCount} MCP server siap dipakai`
        : "belum ada MCP server yang dikonfigurasi";
      pushAgentLiveItem({
        role: "tool",
        tone: "default",
        text: `Capability check: ${memoryLabel}, ${mcpLabel}.`,
        meta: caps.supports.autonomous_mcp_loop ? "autonomous tool loop aktif" : null,
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
      pushAgentLiveItem({ role: "assistant", tone: "working", text: "Aku nemu beberapa issue, jadi aku lanjut pass kedua buat beresin sisanya." });

      const validationReport = validation && !validation.ok ? formatValidationReport(validation, 6000) : null;
      const previewAuditReport = previewAudit && previewAudit.issues.length > 0 ? formatPreviewAuditReport(previewAudit, 3500) : null;

      const repairRes = await runAgentPass(
        buildRepairPrompt(buildMode, agentInput, validationReport, previewAuditReport),
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
    if (finalToast.kind === "success") {
      pushAgentLiveItem({ role: "assistant", tone: "success", text: "Sip, jalur utamanya udah beres. Tinggal kamu review hasil akhirnya." });
      notify(finalToast);
    } else if (finalToast.kind === "warning") {
      pushAgentLiveItem({ role: "assistant", tone: "error", text: "Perubahannya udah kepasang, tapi masih ada beberapa hal yang menurutku perlu review manual." });
      notify(finalToast);
    } else {
      notify(finalToast);
    }
    setAgentStatus("idle");
  } catch (error) {
    setEditorStatus("Agent failed");
    setAgentStatus("error");
    pushAgentLiveItem({ role: "assistant", tone: "error", text: "Aku mentok di tengah jalan. Coba cek error ini dulu ya.", meta: errorMessage(error) });
    notify({ kind: "error", message: `Agent error: ${errorMessage(error)}` });
  } finally {
    setWorkingMsg("");
  }
}
