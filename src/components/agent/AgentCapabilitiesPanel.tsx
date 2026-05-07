import React from "react";
import { Brain, Eye, PlugZap, ShieldCheck } from "lucide-react";
import type { AgentCapabilities } from "../../api";

interface AgentCapabilitiesPanelProps {
  capabilities: AgentCapabilities | null;
  compact?: boolean;
}

function tone(ok: boolean) {
  return ok ? "connected" : "disconnected";
}

export const AgentCapabilitiesPanel: React.FC<AgentCapabilitiesPanelProps> = ({ capabilities, compact = false }) => {
  if (!capabilities) {
    return (
      <div className="missionCard agentCapabilityCard">
        <div className="missionCardHeader">
          <div>
            <div className="missionCardEyebrow">Agent readiness</div>
            <div className="missionCardTitle">Checking runtime</div>
          </div>
          <ShieldCheck size={16} />
        </div>
        <div className="missionEmpty">Capability check belum tersedia untuk project ini.</div>
      </div>
    );
  }

  const mcpCount = capabilities.discovered_mcp_servers.length;
  const memoryLabel = capabilities.supports.supabase_rag_ready
    ? "Supabase RAG"
    : capabilities.supports.vector_memory_retrieval
      ? "Local vector"
      : "Session memory";
  const auditLabel = capabilities.supports.browser_dom_audit
    ? "Browser audit"
    : capabilities.supports.playwright_preview_audit
      ? "Playwright audit"
      : "HTML audit";

  const rows = [
    {
      icon: <Brain size={14} />,
      title: "Memory",
      meta: `${memoryLabel} • ${capabilities.memory.project_entries} project / ${capabilities.memory.session_entries} session`,
      ok: capabilities.supports.long_term_memory_rag || capabilities.supports.short_term_memory_rag,
    },
    {
      icon: <PlugZap size={14} />,
      title: "Tools",
      meta: mcpCount > 0
        ? `${mcpCount} MCP server • ${capabilities.supports.tool_actions.join(", ")}`
        : `Local tools • ${capabilities.supports.tool_actions.join(", ")}`,
      ok: capabilities.supports.skill_registry && capabilities.supports.read_only_inspection_boundary,
    },
    {
      icon: <Eye size={14} />,
      title: "Preview QA",
      meta: `${auditLabel} • ${capabilities.stack.preview_audit_mode}`,
      ok: capabilities.supports.preview_quality_checks || capabilities.supports.playwright_preview_audit,
    },
    {
      icon: <ShieldCheck size={14} />,
      title: "Hosted guard",
      meta: [
        capabilities.supports.command_conversation_boundary ? "chat boundary" : null,
        capabilities.supports.provider_fallback_routing ? "provider fallback" : null,
        capabilities.supports.deep_work_preflight ? "deep preflight" : null,
      ].filter(Boolean).join(" • ") || "basic guard",
      ok: capabilities.supports.command_conversation_boundary && capabilities.supports.deep_work_preflight,
    },
  ];

  return (
    <div className={`missionCard agentCapabilityCard ${compact ? "compact" : ""}`}>
      <div className="missionCardHeader">
        <div>
          <div className="missionCardEyebrow">Agent readiness</div>
          <div className="missionCardTitle">Runtime siap kerja</div>
        </div>
        <div className={`previewStatusPill ${capabilities.ok ? "live" : "idle"}`}>{capabilities.runtime}</div>
      </div>
      <div className="missionCompactList">
        {rows.map((row) => (
          <div key={row.title} className="missionCompactItem static agentCapabilityRow">
            <div className={`agentCapabilityIcon ${tone(Boolean(row.ok))}`}>{row.icon}</div>
            <div>
              <div className="missionCompactPrimary">{row.title}</div>
              <div className="missionCompactMeta">{row.meta}</div>
            </div>
          </div>
        ))}
      </div>
      {capabilities.memory.supabase_warning ? (
        <div className="settingsSubtle compactHint">{capabilities.memory.supabase_warning}</div>
      ) : null}
    </div>
  );
};
