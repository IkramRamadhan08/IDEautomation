import type { Dispatch, SetStateAction } from "react";
import {
  agentHarnessRunShell,
  agentHarnessApply,
  auditPreview,
  fetchAgentCapabilities,
  streamAgent,
  validateProject,
  type AgentIntent,
  type AgentRunTrace,
  type PreviewAuditResult,
  type ProjectValidationRun,
  type TerminalRunResult,
} from "../api";
import type { AgentAction, AgentAuditSnapshot, AgentChange, AgentLiveItem, BuildMode, FileBuffer } from "../types";
import { buildApplyConflictRepairPrompt, buildRepairPrompt, buildVerifierRepairPrompt, getAgentRunPlan } from "./runtime";

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

type RunEvidence = {
  validationRuns: AgentAuditSnapshot["validationRuns"];
  appliedPatches: AgentAuditSnapshot["appliedPatches"];
  shellRuns: AgentAuditSnapshot["shellRuns"];
  previewAudits: AgentAuditSnapshot["previewAudits"];
  repairPasses: AgentAuditSnapshot["repairPasses"];
  commandPolicyDecisions: AgentAuditSnapshot["commandPolicyDecisions"];
};

type BackendExecutionResult = {
  auto_execute?: boolean;
  skipped?: boolean;
  reason?: unknown;
  apply?: Record<string, unknown> | null;
  shell?: Record<string, unknown> | null;
  validation?: ProjectValidationRun | null;
};

type ApplyPreflightConflict = {
  path: string;
  reason: string;
  detail: string;
};

type ApplyPreflightWarning = {
  path: string;
  reason: string;
  detail: string;
};

class ApplyPreflightError extends Error {
  conflicts: ApplyPreflightConflict[];
  warnings: ApplyPreflightWarning[];

  constructor(conflicts: ApplyPreflightConflict[], warnings: ApplyPreflightWarning[]) {
    const conflictText = formatApplyPreflightConflicts(conflicts);
    super(`Apply preflight failed: ${conflictText || "file conflict"}`);
    this.name = "ApplyPreflightError";
    this.conflicts = conflicts;
    this.warnings = warnings;
  }
}

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
  appendAssistantLiveText: (chunk: string, tone?: AgentLiveItem["tone"], exact?: boolean) => void;
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
  executing_apply: "Backend harness menerapkan patch…",
  executing_shell: "Backend harness menjalankan command…",
  executing_validation: "Backend harness memvalidasi hasil…",
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

function classifyCommandFailureText(text: string): string {
  const raw = String(text || "");
  const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const lower = raw.toLowerCase();
  const hints: string[] = [];

  const fileLineMatches = Array.from(raw.matchAll(/(?:^|\s)([\w./-]+\.(?:ts|tsx|js|jsx|css|json|html|py))[:(](\d+)(?::(\d+))?/g))
    .map((match) => `${match[1]}:${match[2]}${match[3] ? `:${match[3]}` : ""}`);
  const uniqueLocations = Array.from(new Set(fileLineMatches)).slice(0, 6);
  if (uniqueLocations.length > 0) hints.push(`Likely files: ${uniqueLocations.join(", ")}`);

  if (/(cannot find module|module not found|could not resolve|failed to resolve import)/i.test(raw)) {
    hints.push("Class: missing import/module or dependency declaration.");
  }
  if (/(not found|command not found|is not recognized|could not determine executable)/i.test(raw)) {
    hints.push("Class: missing project tool/dependencies; install or use the package manager before patching app code.");
  }
  if (/(ts\d{4}|type .* is not assignable|property .* does not exist|implicitly has an 'any' type)/i.test(raw)) {
    hints.push("Class: TypeScript compile error; patch the referenced file/type, then rerun validation.");
  }
  if (/(eslint|lint|react hooks|jsx-a11y|no-unused-vars|prefer-const)/i.test(raw)) {
    hints.push("Class: lint/static analysis error; make the smallest code cleanup that satisfies the rule.");
  }
  if (/(syntaxerror|unexpected token|unterminated|string literal|parse error)/i.test(raw)) {
    hints.push("Class: syntax/parse error; inspect the nearest referenced line before changing behavior.");
  }
  if (/(eaddrinuse|port .* already in use|address already in use)/i.test(raw)) {
    hints.push("Class: preview/runtime port conflict; avoid app code churn unless the command itself is wrong.");
  }
  if (/(enoent|no such file or directory)/i.test(raw)) {
    hints.push("Class: missing file/path; verify generated paths and imports.");
  }

  const highSignal = lines
    .filter((line) => (
      /error|failed|cannot|not found|ts\d{4}|eslint|syntax|unexpected|enoent|eaddrinuse/i.test(line)
      && !/^\$ /.test(line)
    ))
    .slice(0, 10);
  if (highSignal.length > 0) hints.push(`High-signal lines:\n- ${highSignal.join("\n- ")}`);

  if (hints.length === 0 && lower.trim()) {
    hints.push(`No classifier matched. Start from the last failing command and shortest error line.\n- ${lines.slice(-6).join("\n- ")}`);
  }
  return hints.join("\n");
}

function classifyValidationFailure(validation: ProjectValidationRun | null): string | null {
  if (!validation || validation.ok) return null;
  const failing = validation.results.filter((result) => !result.ok);
  if (failing.length === 0) return null;
  return failing.map((result, index) => [
    `Failure ${index + 1}: ${result.command} (exit ${result.returncode})`,
    classifyCommandFailureText(`${result.stdout || ""}\n${result.stderr || ""}`),
  ].filter(Boolean).join("\n")).join("\n\n");
}

function classifyShellFailure(results: ShellActionRun[]): string | null {
  const failing = results.filter((result) => !result.ok);
  if (failing.length === 0) return null;
  return failing.map((result, index) => [
    `Failure ${index + 1}: ${result.command} (exit ${result.returncode})`,
    result.policy && !result.policy.ok ? `Policy: ${result.policy.risk_level} - ${result.policy.reason}` : null,
    result.error ? `Execution error: ${result.error}` : null,
    classifyCommandFailureText(`${result.stdout || ""}\n${result.stderr || ""}`),
  ].filter(Boolean).join("\n")).join("\n\n");
}

function formatApplyPreflightConflicts(conflicts: ApplyPreflightConflict[], maxItems = 8) {
  return conflicts
    .slice(0, maxItems)
    .map((item) => item.detail || `${item.path}: ${item.reason}`)
    .join("; ");
}

function formatApplyPreflightReport(error: ApplyPreflightError, maxChars = 4000) {
  const conflicts = error.conflicts.length > 0
    ? `conflicts:\n- ${error.conflicts.map((item) => `${item.path}: ${item.reason} - ${item.detail}`).join("\n- ")}`
    : "conflicts: none";
  const warnings = error.warnings.length > 0
    ? `warnings:\n- ${error.warnings.map((item) => `${item.path}: ${item.reason} - ${item.detail}`).join("\n- ")}`
    : null;
  const report = [conflicts, warnings].filter(Boolean).join("\n\n");
  return report.length > maxChars ? `${report.slice(0, maxChars)}\n…[truncated]` : report;
}

function isApplyPreflightError(error: unknown): error is ApplyPreflightError {
  return error instanceof ApplyPreflightError;
}

function toolEventName(data: Record<string, unknown>) {
  const kind = typeof data.kind === "string" ? data.kind : "tool";
  const server = typeof data.server === "string" ? data.server : "";
  const tool = typeof data.tool === "string" ? data.tool : "";
  if (kind === "mcp" && server && tool) return `${server}.${tool}`;
  return tool || kind;
}

function formatPreviewAuditReport(audit: PreviewAuditResult, maxChars = 4000) {
  const issueDetails = audit.issue_details && audit.issue_details.length > 0
    ? `issue_details:\n- ${audit.issue_details.map((issue) => `${issue.severity}: ${issue.category} - ${issue.detail}${issue.suggested_fix ? ` (fix: ${issue.suggested_fix})` : ""}`).join("\n- ")}`
    : null;
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
    issueDetails,
  ].filter(Boolean);

  if (audit.excerpt.trim()) {
    sections.push(`excerpt:\n${audit.excerpt.trim()}`);
  }

  const report = sections.join("\n\n");
  return report.length > maxChars ? `${report.slice(0, maxChars)}\n…[truncated]` : report;
}

function previewBlockingIssueCount(audit: PreviewAuditResult | null) {
  if (!audit) return 0;
  const details = audit.issue_details || [];
  if (details.length === 0) return audit.ok ? 0 : audit.issues.length;
  return details.filter((issue) => issue.severity === "blocking").length;
}

function previewWarningIssueCount(audit: PreviewAuditResult | null) {
  if (!audit) return 0;
  return (audit.issue_details || []).filter((issue) => issue.severity === "warning").length;
}

function toAuditSnapshot(label: string, trace: AgentRunTrace, makeId: () => string, evidence?: RunEvidence): AgentAuditSnapshot {
  return {
    id: makeId(),
    label,
    passes: trace.passes,
    contextFiles: trace.context_files || [],
    finalConfidence: trace.final_confidence,
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
    validationRuns: evidence?.validationRuns || [],
    appliedPatches: evidence?.appliedPatches || [],
    shellRuns: evidence?.shellRuns || [],
    previewAudits: evidence?.previewAudits || [],
    repairPasses: evidence?.repairPasses || [],
    commandPolicyDecisions: evidence?.commandPolicyDecisions || [],
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

function installCommandForMissingProjectTool(validation: ProjectValidationRun | null): AgentAction | null {
  if (!validation || validation.ok) return null;
  const joined = validation.results
    .filter((result) => !result.ok)
    .map((result) => `${result.command}\n${result.stdout || ""}\n${result.stderr || ""}`)
    .join("\n")
    .toLowerCase();
  if (!joined) return null;
  const looksMissingTool = [
    "not found",
    "command not found",
    "is not recognized",
    "cannot find module",
    "could not determine executable",
    "missing script",
  ].some((token) => joined.includes(token));
  if (!looksMissingTool) return null;
  const command = validation.results.map((result) => result.command).join(" && ");
  if (/\bpnpm\b/i.test(command)) return { type: "shell", command: "pnpm install", reason: "Validation failed because project dependencies/tools are missing." };
  if (/\byarn\b/i.test(command)) return { type: "shell", command: "yarn install", reason: "Validation failed because project dependencies/tools are missing." };
  if (/\bbun\b/i.test(command)) return { type: "shell", command: "bun install", reason: "Validation failed because project dependencies/tools are missing." };
  if (/\bnpm\b/i.test(command) || /\b(tsc|vite|eslint|vitest)\b/i.test(joined)) {
    return { type: "shell", command: "npm install", reason: "Validation failed because project dependencies/tools are missing." };
  }
  return null;
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
  const evidence: RunEvidence = {
    validationRuns: [],
    appliedPatches: [],
    shellRuns: [],
    previewAudits: [],
    repairPasses: [],
    commandPolicyDecisions: [],
  };
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

  const refreshAuditTrailEvidence = () => {
    setAgentAuditTrail((prev) => prev.map((snapshot) => ({
      ...snapshot,
      validationRuns: evidence.validationRuns,
      appliedPatches: evidence.appliedPatches,
      shellRuns: evidence.shellRuns,
      previewAudits: evidence.previewAudits,
      repairPasses: evidence.repairPasses,
      commandPolicyDecisions: evidence.commandPolicyDecisions,
    })));
  };

  const recordBackendExecutionEvidence = (res: { execution?: BackendExecutionResult }, sourceChanges: AgentChange[]): { shellResults: ShellActionRun[]; validation: ProjectValidationRun | null } => {
    const execution = res.execution;
    if (!execution || execution.auto_execute !== true) return { shellResults: [], validation: null };
    if (execution.skipped) {
      pushAgentLiveItem({
        role: "tool",
        tone: "error",
        text: "Backend auto-execute dilewati karena verifier belum aman.",
        meta: typeof execution.reason === "string" ? execution.reason : "execution skipped",
      });
      return { shellResults: [], validation: null };
    }
    const apply = execution.apply && typeof execution.apply === "object" ? execution.apply as Record<string, unknown> : null;
    if (apply && apply.applied === true) {
      const paths = Array.isArray(apply.paths) ? apply.paths.map(String) : sourceChanges.map((change) => change.path);
      const checkpointPath = typeof apply.checkpoint_path === "string" ? apply.checkpoint_path : null;
      evidence.appliedPatches?.push({
        label: "Backend auto execute",
        count: typeof apply.count === "number" ? apply.count : paths.length,
        paths,
        checkpointPath,
      });
      pushAgentLiveItem({
        role: "tool",
        tone: "success",
        text: `Backend auto-execute sudah apply ${paths.length} file.`,
        meta: checkpointPath ? `checkpoint ${checkpointPath}` : null,
      });
    }
    const shell = execution.shell && typeof execution.shell === "object" ? execution.shell as Record<string, unknown> : null;
    const shellResults = Array.isArray(shell?.results) ? shell.results as ShellActionRun[] : [];
    for (const run of shellResults) {
      evidence.shellRuns?.push({
        command: String(run.command || ""),
        ok: run.ok === true,
        returncode: typeof run.returncode === "number" ? run.returncode : null,
        stdoutPreview: String(run.stdout || "").slice(0, 500),
        stderrPreview: String(run.stderr || "").slice(0, 500),
        error: run.error || null,
      });
      const policy = run.policy;
      if (policy) {
        evidence.commandPolicyDecisions?.push({
          command: policy.command,
          riskLevel: policy.risk_level,
          ok: policy.ok,
          reason: policy.reason,
        });
      }
    }
    if (shellResults.length > 0) {
      pushAgentLiveItem({
        role: "tool",
        tone: shellResults.every((run) => run.ok) ? "success" : "error",
        text: `Backend auto-execute menjalankan ${shellResults.length} command.`,
        meta: shellResults.map((run) => String(run.command || "")).join(" • "),
      });
    }
    const validation = execution.validation && typeof execution.validation === "object" ? execution.validation as ProjectValidationRun : null;
    if (validation) {
      evidence.validationRuns?.push({
        label: "Backend validation",
        ok: validation.ok,
        ran: validation.ran,
        failed: validation.failed,
        commands: validation.commands,
      });
      pushAgentLiveItem({
        role: "tool",
        tone: validation.ok ? "success" : "error",
        text: validation.ok ? "Backend validation lolos." : "Backend validation nemu problem yang perlu repair.",
        meta: validation.commands.join(" • ") || null,
      });
    }
    refreshAuditTrailEvidence();
    return { shellResults, validation };
  };

  const runAgentPass = async (prompt: string, passEditorStatus: string, resetReply = true, autoExecute = false) => {
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

        if (event.event === "tool_call") {
          const name = toolEventName(event.data);
          const phase = typeof event.data.phase === "string" ? event.data.phase : "tooling";
          pushAgentLiveItem({
            role: "tool",
            tone: "working",
            text: `Tool call: ${name}`,
            meta: phase,
          });
          return;
        }

        if (event.event === "tool_output") {
          const name = toolEventName(event.data);
          const ok = event.data.ok === true;
          const duration = typeof event.data.duration_ms === "number" ? `${Math.round(event.data.duration_ms)}ms` : "";
          const error = typeof event.data.error === "string" ? event.data.error : "";
          pushAgentLiveItem({
            role: "tool",
            tone: ok ? "success" : "error",
            text: ok ? `Tool output: ${name} selesai.` : `Tool output: ${name} gagal.`,
            meta: [duration, error].filter(Boolean).join(" • ") || null,
          });
          return;
        }

        if (event.event === "delta") {
          const spokenChunk = typeof event.data.spoken_chunk === "string" ? event.data.spoken_chunk : "";
          const nativeStream = event.data.native_stream === true;
          const message = typeof event.data.message === "string" ? event.data.message : "";
          if (spokenChunk) {
            setAgentReply((prev) => {
              if (!prev) return nativeStream ? spokenChunk.trimStart() : spokenChunk;
              return nativeStream ? `${prev}${spokenChunk}` : `${prev} ${spokenChunk}`;
            });
            appendAssistantLiveText(spokenChunk, "default", nativeStream);
          }
          if (message) {
            setWorkingMsg(message);
          }
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
      autoExecute,
    );
  };

  const applyChanges = async (changes: AgentChange[], statusLabel = "Applying") => {
    if (changes.length === 0) return;
    setWorkingMsg(`Menerapkan ${changes.length} perubahan…`);
    setEditorStatus(`${statusLabel} ${changes.length} file changes...`);
    pushAgentLiveItem({
      role: "tool",
      tone: "working",
      text: `Aku kirim ${changes.length} file ke backend apply harness.`,
      meta: statusLabel,
    });
    const applyRes = await agentHarnessApply(
      changes.map((change) => ({
        path: change.path,
        content: change.new_content,
        diff: change.diff || null,
        expected_sha256: change.old_sha256 || null,
        expected_exists: typeof change.old_exists === "boolean" ? change.old_exists : null,
      })),
      selectedProject,
      statusLabel,
    );
    if (!applyRes.ok || !applyRes.applied) {
      const conflicts = applyRes.conflicts || [];
      const warnings = applyRes.warnings || [];
      const conflictText = formatApplyPreflightConflicts(conflicts);
      pushAgentLiveItem({
        role: "tool",
        tone: "error",
        text: "Backend apply harness membatalkan patch karena file berubah sebelum apply.",
        meta: conflictText || "conflict",
      });
      throw new ApplyPreflightError(conflicts, warnings);
    }
    if ((applyRes.warnings || []).length > 0) {
      pushAgentLiveItem({
        role: "tool",
        tone: "working",
        text: "Backend apply harness lolos dengan catatan.",
        meta: (applyRes.warnings || []).map((item) => item.detail || item.reason).slice(0, 4).join("; "),
      });
    }
    evidence.appliedPatches?.push({
      label: statusLabel,
      count: applyRes.count,
      paths: applyRes.paths || changes.map((change) => change.path),
      checkpointPath: applyRes.checkpoint_path,
    });
    refreshAuditTrailEvidence();
    pushAgentLiveItem({
      role: "tool",
      tone: "success",
      text: `Backend apply harness menerapkan ${applyRes.count} file.`,
      meta: `checkpoint ${applyRes.checkpoint_path}`,
    });
    syncBuffers(changes);
    await refreshExplorer();
  };

  const applyChangesWithConflictRepair = async (
    initialChanges: AgentChange[],
    initialActions: AgentAction[],
    statusLabel = "Applying",
  ): Promise<{ changes: AgentChange[]; actions: AgentAction[] }> => {
    let nextChanges = initialChanges;
    let nextActions = initialActions;
    const maxConflictRepairPasses = buildMode === "full-agent" ? 2 : 1;

    for (let pass = 0; pass <= maxConflictRepairPasses; pass += 1) {
      try {
        await applyChanges(nextChanges, pass === 0 ? statusLabel : "Applying conflict repair");
        return { changes: nextChanges, actions: nextActions };
      } catch (error) {
        if (!isApplyPreflightError(error) || pass >= maxConflictRepairPasses) {
          throw error;
        }

        const repairPass = pass + 1;
        const conflictReport = formatApplyPreflightReport(error);
        appendLogSection(`APPLY PREFLIGHT CONFLICT ${repairPass}`, conflictReport);
        setWorkingMsg(`Patch conflict, meminta agent rebase perubahan ${repairPass}/${maxConflictRepairPasses}…`);
        setEditorStatus(`Rebasing agent patch (${repairPass}/${maxConflictRepairPasses})...`);
        pushAgentLiveItem({
          role: "tool",
          tone: "working",
          text: `Patch conflict repair ${repairPass}/${maxConflictRepairPasses}: agent baca ulang file terbaru lalu bikin patch baru.`,
          meta: formatApplyPreflightConflicts(error.conflicts) || "file conflict",
        });

        const repairRes = await runAgentPass(
          buildApplyConflictRepairPrompt(buildMode, agentInput, conflictReport, repairPass, maxConflictRepairPasses),
          `Rebasing agent patch (${repairPass}/${maxConflictRepairPasses})...`,
          false,
        );
        appendLogSection(`APPLY CONFLICT REPAIR PASS ${repairPass}`, repairRes.log);
        if (repairRes.spoken) setAgentReply(repairRes.spoken);
        const repairTrace = repairRes.trace;
        pushRunTrace(pushAgentLiveItem, repairTrace);
        evidence.repairPasses?.push({
          label: `Apply conflict repair ${repairPass}`,
          producedChanges: (repairRes.changes || []).length,
          producedActions: (repairRes.actions || []).length,
          verifierFailures: failedVerifierChecks(repairTrace).length,
        });
        if (repairTrace) {
          setAgentAuditTrail((prev) => [...prev, toAuditSnapshot(`Apply conflict repair ${repairPass}`, repairTrace, makeAgentLiveId, evidence)]);
        } else {
          refreshAuditTrailEvidence();
        }

        const verifierFailures = blockingVerifierChecks(repairTrace);
        if (verifierFailures.length > 0) {
          throw new Error(`Apply conflict repair failed verifier: ${verifierFailureSummary(repairTrace)}`);
        }

        nextChanges = repairRes.changes || [];
        nextActions = [...nextActions, ...(repairRes.actions || [])];

        if (nextChanges.length === 0) {
          throw new Error("Apply conflict repair did not produce replacement file changes.");
        }
      }
    }

    return { changes: nextChanges, actions: nextActions };
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
      const reason = typeof action.reason === "string" ? action.reason : "Agent requested project command.";
      try {
        pushAgentLiveItem({ role: "tool", tone: "working", text: "Aku serahin command ke backend harness biar policy, cwd, dan sync file ditangani satu tempat.", meta: action.command });
        const harness = await agentHarnessRunShell(
          [{ command: action.command, cwd: selectedProject !== "." ? selectedProject : undefined, reason }],
          selectedProject,
        );
        const runRes = harness.results[0];
        if (!runRes) {
          throw new Error("Backend harness did not return a shell result.");
        }
        const policy = runRes.policy;
        results.push({ command: action.command, ...runRes });
        evidence.commandPolicyDecisions?.push({
          command: policy?.command || action.command,
          riskLevel: policy?.risk_level || (runRes.ok ? "safe" : "blocked"),
          ok: runRes.ok,
          reason: policy?.reason || (runRes.ok ? "Backend harness allowed command." : runRes.stderr || "Backend harness blocked command."),
        });
        refreshAuditTrailEvidence();
        if (!runRes.ok && runRes.returncode === 126) {
          evidence.shellRuns?.push({
            command: action.command,
            ok: false,
            returncode: runRes.returncode,
            stdoutPreview: "",
            stderrPreview: runRes.stderr || policy?.reason || "",
            error: policy && !policy.ok ? "Blocked by command policy" : "Blocked by backend harness",
          });
          refreshAuditTrailEvidence();
          pushAgentLiveItem({
            role: "tool",
            tone: "error",
            text: "Command ditahan guarded autonomy.",
            meta: `${policy?.risk_level || "blocked"}: ${policy?.reason || runRes.stderr}`,
          });
          continue;
        }
        evidence.shellRuns?.push({
          command: action.command,
          ok: runRes.ok,
          returncode: runRes.returncode,
          stdoutPreview: (runRes.stdout || "").slice(0, 500),
          stderrPreview: (runRes.stderr || "").slice(0, 500),
          error: null,
        });
        refreshAuditTrailEvidence();
        appendLogSection(runRes.ok ? "TERMINAL STDOUT" : "TERMINAL STDERR", runRes.ok ? runRes.stdout : runRes.stderr);
        pushAgentLiveItem({
          role: "tool",
          tone: runRes.ok ? "success" : "error",
          text: runRes.ok ? "Backend harness selesai jalanin command." : "Backend harness jalanin command, tapi hasilnya error.",
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
        evidence.shellRuns?.push({
          command: action.command,
          ok: false,
          returncode: 1,
          stdoutPreview: "",
          stderrPreview: "",
          error: message,
        });
        refreshAuditTrailEvidence();
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
    evidence.validationRuns?.push({
      label,
      ok: validation.ok,
      ran: validation.ran,
      failed: validation.failed,
      commands: validation.commands,
    });
    refreshAuditTrailEvidence();
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
    const blocking = previewBlockingIssueCount(audit);
    const warnings = previewWarningIssueCount(audit);
    evidence.previewAudits?.push({
      label,
      ok: audit.ok,
      auditMode: audit.audit_mode,
      blocking,
      warnings,
      summary: audit.summary,
    });
    refreshAuditTrailEvidence();
    appendLogSection(label, formatPreviewAuditReport(audit));
    pushAgentLiveItem({
      role: "tool",
      tone: blocking === 0 ? "success" : "error",
      text: blocking === 0 ? `Preview tidak punya blocker. Warning: ${warnings}.` : `Preview audit nemu ${blocking} blocker yang harus dirapihin.`,
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
      text: buildMode === "full-agent"
        ? "Free-tier guard aktif, tapi Clara tetap boleh repair hasil build yang gagal supaya full preview lebih reliable."
        : "Free-tier guard aktif: agent hemat panggilan model dan context supaya limit provider nggak cepat mentok.",
      meta: buildMode === "full-agent" ? "Full Preview: maksimal 2 repair" : "Raka: maksimal 1 repair untuk validasi/shell",
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
    const res = await runAgentPass(
      agentInput,
      requestEditorStatus,
      true,
      inputIntent.kind === "command" || inputIntent.kind === "mixed",
    );
    setAgentReply(res.spoken);
    setAgentLog(res.log);

    let changes: AgentChange[] = res.changes || [];
    let actions: AgentAction[] = res.actions || [];
    const backendAutoExecuted = res.execution?.auto_execute === true && res.execution?.ok !== false && !res.execution?.skipped;
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
      setAgentAuditTrail([toAuditSnapshot("Main pass", mainTrace, makeAgentLiveId, evidence)]);
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
        setAgentAuditTrail((prev) => [...prev, toAuditSnapshot("Verifier repair pass", verifierRepairTrace, makeAgentLiveId, evidence)]);
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

    let shellResults: ShellActionRun[] = [];
    let backendValidation: ProjectValidationRun | null = null;
    if (backendAutoExecuted) {
      const backendEvidence = recordBackendExecutionEvidence(res, changes);
      shellResults = backendEvidence.shellResults;
      backendValidation = backendEvidence.validation;
      if (changes.length > 0) {
        syncBuffers(changes);
        await refreshExplorer();
      }
    } else if (outputSafeToApply && changes.length > 0) {
      const applied = await applyChangesWithConflictRepair(changes, actions);
      changes = applied.changes;
      actions = applied.actions;
      combinedActions = [...actions];
      setAgentActions(combinedActions);
    }

    if (!backendAutoExecuted) {
      shellResults = outputSafeToApply && actions.length > 0
      ? await runShellActionsForPass(actions)
      : [];
    }

    const auditedPreviewUrl = outputSafeToApply && changes.length > 0 ? await refreshPreviewSurface() : currentPreviewUrl;

    let validation: ProjectValidationRun | null = backendValidation;
    let previewAudit: PreviewAuditResult | null = null;

    if ((resolvedIntent.kind === "conversation" || resolvedIntent.kind === "inspection") && changes.length === 0 && actions.length === 0) {
      finalStatus = resolvedIntent.kind === "inspection"
        ? "Inspection answered without file changes"
        : "Conversation answered without file changes";
      finalToast = { kind: "success", message: "Jawaban siap, tidak ada perubahan file" };
    }

    if (!validation && outputSafeToApply && (changes.length > 0 || actions.length > 0) && shouldRunValidation) {
      validation = await runValidationPass("VALIDATION PASS 1");
    }
    if (outputSafeToApply && validation && !validation.ok) {
      const installAction = installCommandForMissingProjectTool(validation);
      if (installAction) {
        pushAgentLiveItem({
          role: "tool",
          tone: "working",
          text: "Validasi gagal karena dependency/tool project belum terpasang. Aku install dependency dulu lalu ulang validasi.",
          meta: String(installAction.command || ""),
        });
        const installResults = await runShellActionsForPass([installAction]);
        shellResults.push(...installResults);
        if (installResults.every((result) => result.ok)) {
          validation = await runValidationPass("VALIDATION PASS 1B");
        }
      }
    }
    if (outputSafeToApply && auditedPreviewUrl && (changes.length > 0 || actions.length > 0) && shouldAuditPreview) {
      previewAudit = await runPreviewAuditPass(auditedPreviewUrl, "PREVIEW AUDIT 1");
    }

    let latestValidation = validation;
    let latestPreviewAudit = previewAudit;
    let latestShellResults = shellResults;
    let latestVerifierFailures = mainVerifierFailures;
    const maxRepairPasses = buildMode === "full-agent" ? 2 : (friendlyFreeTierMode ? 1 : 2);
    const needsRepair = () => {
      const validationFailing = Boolean(latestValidation && !latestValidation.ok);
      const shellFailing = latestShellResults.some((result) => !result.ok);
      const previewFailing = previewBlockingIssueCount(latestPreviewAudit) > 0;
      return shellFailing || validationFailing || (buildMode === "full-agent" || !friendlyFreeTierMode ? previewFailing : false);
    };
    const hasValidationIssues = Boolean(latestValidation && !latestValidation.ok);
    const hasPreviewIssues = previewBlockingIssueCount(latestPreviewAudit) > 0;

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
          meta: buildMode === "full-agent" ? "Full Preview reliability loop: maksimal 2 repair" : (friendlyFreeTierMode ? "free-tier guard: maksimal 1 repair" : "bounded loop: maksimal 2 repair"),
        });

        const validationReport = latestValidation && !latestValidation.ok ? formatValidationReport(latestValidation, 6000) : null;
        const shellReport = latestShellResults.some((result) => !result.ok) ? formatShellActionReport(latestShellResults, 6000) : null;
        const previewAuditReport = latestPreviewAudit && latestPreviewAudit.issues.length > 0 ? formatPreviewAuditReport(latestPreviewAudit, 3500) : null;
        const failureDiagnosis = [
          classifyValidationFailure(latestValidation),
          classifyShellFailure(latestShellResults),
        ].filter(Boolean).join("\n\n") || null;
        if (failureDiagnosis) {
          pushAgentLiveItem({
            role: "tool",
            tone: "working",
            text: "Appora mengklasifikasi error sebelum repair.",
            meta: failureDiagnosis.slice(0, 420),
          });
        }

        const repairRes = await runAgentPass(
          buildRepairPrompt(buildMode, agentInput, validationReport, previewAuditReport, shellReport, failureDiagnosis, pass, maxRepairPasses),
          `Fixing preview and validation issues (${pass}/${maxRepairPasses})...`,
        );

        appendLogSection(`REPAIR PASS ${pass}`, repairRes.log);
        if (repairRes.spoken) setAgentReply(repairRes.spoken);
        const repairTrace = repairRes.trace;
        pushRunTrace(pushAgentLiveItem, repairTrace);
        evidence.repairPasses?.push({
          label: `Repair pass ${pass}`,
          producedChanges: (repairRes.changes || []).length,
          producedActions: (repairRes.actions || []).length,
          verifierFailures: failedVerifierChecks(repairTrace).length,
        });
        if (repairTrace) {
          setAgentAuditTrail((prev) => [...prev, toAuditSnapshot(`Repair pass ${pass}`, repairTrace, makeAgentLiveId, evidence)]);
        } else {
          refreshAuditTrailEvidence();
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

        let repairChanges: AgentChange[] = repairRes.changes || [];
        let repairActions: AgentAction[] = repairRes.actions || [];
        const combinedActionsBeforeRepair = combinedActions;
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
          const appliedRepair = await applyChangesWithConflictRepair(repairChanges, repairActions, "Applying repair");
          repairChanges = appliedRepair.changes;
          repairActions = appliedRepair.actions;
          combinedActions = [...combinedActionsBeforeRepair, ...repairActions];
          setAgentActions(combinedActions);
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
      const previewStillFailing = previewBlockingIssueCount(latestPreviewAudit) > 0;
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
