import React from "react";
import {
  Bot,
  CheckCircle2,
  CircleDashed,
  Code2,
  Eye,
  FileCheck2,
  ShieldAlert,
  TerminalSquare,
  User,
  Wrench,
  XCircle,
} from "lucide-react";
import { type AgentLiveItem } from "../../../shared/types";

interface AgentLiveStageProps {
  items: AgentLiveItem[];
  agentStatus: "idle" | "thinking" | "error";
  personaName?: string;
  workingMsg?: string;
  emptyText?: string;
  compact?: boolean;
  includeTools?: boolean;
  includeTraceTools?: boolean;
  conversationOnly?: boolean;
  showTyping?: boolean;
  hideEmpty?: boolean;
}

const LOW_SIGNAL_TOOL_PREFIXES = [
  "intent kebaca",
  "backend intent",
  "run trace",
  "memory yang kepake",
  "skill yang dipilih",
  "capability check",
  "free-tier guard",
  "agent run ini punya warning",
];

function roleLabel(item: AgentLiveItem, personaName: string) {
  if (item.role === "user") return "You";
  if (item.role === "tool") return "Action";
  return personaName;
}

function roleIcon(item: AgentLiveItem) {
  if (item.role === "user") return <User size={13} />;
  if (item.role === "tool") return <Wrench size={13} />;
  return <Bot size={13} />;
}

function valueAsString(value: unknown) {
  return typeof value === "string" ? value : "";
}

function valueAsNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function compactText(value: unknown, max = 180) {
  const clean = String(value || "").replace(/\s+/g, " ").trim();
  if (!clean) return "";
  return clean.length > max ? `${clean.slice(0, max - 1).trimEnd()}…` : clean;
}

function inferToolKind(item: AgentLiveItem) {
  if (item.kind) return item.kind;
  const text = `${item.text} ${item.meta || ""}`.toLowerCase();
  if (text.includes("command") || text.includes("terminal") || text.includes("shell")) return "shell";
  if (text.includes("preview")) return "preview";
  if (text.includes("validasi") || text.includes("validation")) return "validation";
  if (text.includes("patch") || text.includes("apply")) return "apply";
  if (text.includes("intent") || text.includes("trace") || text.includes("memory") || text.includes("skill")) return "trace";
  if (text.includes("tool call")) return "tool_call";
  if (text.includes("tool output")) return "tool_output";
  return "system";
}

function isLowSignalToolItem(item: AgentLiveItem) {
  if (item.role !== "tool") return false;
  if (item.tone === "error") return false;
  const kind = inferToolKind(item);
  if (["shell", "preview", "validation", "apply", "tool_call", "tool_output"].includes(kind)) return false;
  const text = item.text.trim().toLowerCase();
  if (LOW_SIGNAL_TOOL_PREFIXES.some((prefix) => text.startsWith(prefix))) return true;
  return item.tone === "default" && (kind === "trace" || kind === "system");
}

function toolIcon(kind: string, tone: AgentLiveItem["tone"]) {
  if (tone === "error") return <XCircle size={15} />;
  if (tone === "success") return <CheckCircle2 size={15} />;
  if (kind === "shell") return <TerminalSquare size={15} />;
  if (kind === "preview") return <Eye size={15} />;
  if (kind === "validation") return <FileCheck2 size={15} />;
  if (kind === "apply") return <Code2 size={15} />;
  if (kind === "tool_call") return <CircleDashed size={15} />;
  return <ShieldAlert size={15} />;
}

function toolTitle(kind: string, item: AgentLiveItem) {
  const data = item.data || {};
  const name = valueAsString(data.name) || valueAsString(data.tool);
  if (kind === "shell") return "Terminal command";
  if (kind === "preview") return "Preview check";
  if (kind === "validation") return "Validation";
  if (kind === "apply") return "File changes";
  if (kind === "tool_call") return name ? `Tool call: ${name}` : "Tool call";
  if (kind === "tool_output") return name ? `Tool result: ${name}` : "Tool result";
  if (kind === "trace") return "Run trace";
  return "Agent activity";
}

function toolStatus(item: AgentLiveItem) {
  if (item.tone === "success") return "Passed";
  if (item.tone === "error") return "Needs attention";
  if (item.tone === "working") return "Running";
  return "Info";
}

function renderToolDetail(kind: string, item: AgentLiveItem) {
  const data = item.data || {};
  const command = valueAsString(data.command) || (kind === "shell" ? valueAsString(item.meta) : "");
  const summary = compactText(data.summary || item.meta || item.text);
  const error = compactText(data.error || data.stderr);
  const stdout = compactText(data.stdout);
  const returncode = valueAsNumber(data.returncode);
  const blocking = valueAsNumber(data.blocking);
  const warnings = valueAsNumber(data.warnings);
  const ran = valueAsNumber(data.ran);
  const failed = valueAsNumber(data.failed);
  const duration = valueAsString(data.duration);

  if (kind === "shell") {
    return (
      <>
        {command ? <code className="agentLiveMono">{command}</code> : null}
        <div className="agentLiveCardFacts">
          {returncode !== null ? <span>exit {returncode}</span> : null}
          {duration ? <span>{duration}</span> : null}
          {valueAsNumber(data.synced_files) ? <span>{valueAsNumber(data.synced_files)} files synced</span> : null}
        </div>
        {error ? <div className="agentLiveCardOutput error">{error}</div> : stdout ? <div className="agentLiveCardOutput">{stdout}</div> : null}
      </>
    );
  }

  if (kind === "preview") {
    return (
      <>
        <div className="agentLiveCardFacts">
          {blocking !== null ? <span>{blocking} blockers</span> : null}
          {warnings !== null ? <span>{warnings} warnings</span> : null}
          {valueAsString(data.audit_mode) ? <span>{valueAsString(data.audit_mode)}</span> : null}
        </div>
        {summary ? <div className="agentLiveCardOutput">{summary}</div> : null}
      </>
    );
  }

  if (kind === "validation") {
    return (
      <>
        <div className="agentLiveCardFacts">
          {ran !== null ? <span>{ran} checks</span> : null}
          {failed !== null ? <span>{failed} failed</span> : null}
        </div>
        {summary ? <div className="agentLiveCardOutput">{summary}</div> : null}
      </>
    );
  }

  return summary ? <div className="agentLiveCardOutput">{summary}</div> : null;
}

function ToolActivityCard({ item }: { item: AgentLiveItem }) {
  const kind = inferToolKind(item);
  const status = toolStatus(item);

  return (
    <div className={`agentLiveCard ${item.tone || "default"} ${kind}`}>
      <div className="agentLiveCardTop">
        <span className="agentLiveCardIcon">{toolIcon(kind, item.tone)}</span>
        <div className="agentLiveCardMain">
          <div className="agentLiveCardTitle">{toolTitle(kind, item)}</div>
          <div className="agentLiveCardSub">{status}</div>
        </div>
      </div>
      {renderToolDetail(kind, item)}
    </div>
  );
}

export const AgentLiveStage: React.FC<AgentLiveStageProps> = ({
  items,
  agentStatus,
  personaName = "Agent",
  workingMsg,
  emptyText = "Run agent untuk lihat jawaban muncul live di sini.",
  compact = false,
  includeTools = true,
  includeTraceTools = false,
  conversationOnly = false,
  showTyping = true,
  hideEmpty = false,
}) => {
  const visibleItems = items.filter((item) => {
    if (!includeTools && item.role === "tool") return false;
    if (!includeTraceTools && isLowSignalToolItem(item)) return false;
    if (!conversationOnly) return true;
    if (item.role === "user") return true;
    return item.role === "assistant" && (item.tone === "default" || item.tone === "error" || !item.tone);
  });

  return (
    <div className={`agentLiveStage ${compact ? "compact" : ""}`}>
      {visibleItems.length === 0 && !hideEmpty ? <div className="agentLiveEmpty">{emptyText}</div> : null}

      {visibleItems.map((item) => (
        item.role === "tool" ? (
          <ToolActivityCard key={item.id} item={item} />
        ) : (
          <div key={item.id} className={`agentLiveBubble ${item.role} ${item.tone || "default"}`}>
            <div className="agentLiveBubbleMeta">
              <span className="agentLiveBubbleIcon">{roleIcon(item)}</span>
              <span>{roleLabel(item, personaName)}</span>
            </div>
            <div className="agentLiveBubbleText">{item.text}</div>
            {item.meta ? <div className="agentLiveBubbleSubtext">{item.meta}</div> : null}
          </div>
        )
      ))}

      {showTyping && agentStatus === "thinking" ? (
        <div className="agentLiveTyping">
          <span className="spinner" />
          <span>{workingMsg || `${personaName} lagi jalan...`}</span>
        </div>
      ) : null}
    </div>
  );
};
