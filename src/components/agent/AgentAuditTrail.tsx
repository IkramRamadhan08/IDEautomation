import React from "react";
import type { AgentAuditSnapshot } from "../../types";

interface AgentAuditTrailProps {
  snapshots: AgentAuditSnapshot[];
  compact?: boolean;
}

function shortText(value: string, max = 140) {
  const clean = value.trim();
  if (clean.length <= max) return clean;
  return `${clean.slice(0, max)}…`;
}

export const AgentAuditTrail: React.FC<AgentAuditTrailProps> = ({ snapshots, compact = false }) => {
  if (snapshots.length === 0) {
    return <div className="missionEmpty">Belum ada audit trail. Begitu agent selesai satu run, jejak reasoning boundary-nya bakal nongol di sini.</div>;
  }

  const ordered = [...snapshots].reverse();

  return (
    <div className={`agentAuditTrail ${compact ? "compact" : ""}`}>
      {ordered.map((snapshot) => (
        <div key={snapshot.id} className="agentAuditStage">
          <div className="agentAuditStageHeader">
            <div>
              <div className="agentAuditStageTitle">{snapshot.label}</div>
              <div className="agentAuditStageMeta">
                {`passes=${snapshot.passes}`} • {`plan=${snapshot.plan?.length || 0}`} • {`verify=${snapshot.verification?.length || 0}`} • {`memory=${snapshot.memoryHits.length}`} • {`skills=${snapshot.skills.length}`} • {`mcp used=${snapshot.mcpToolsUsed.length}`}
              </div>
            </div>
          </div>

          {snapshot.plan && snapshot.plan.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Execution plan</div>
              <div className="agentAuditList">
                {snapshot.plan.slice(0, compact ? 3 : 6).map((item, index) => (
                  <div key={`${snapshot.id}-plan-${index}`} className="agentAuditRow">
                    <div className="agentAuditPrimary">{item.title}</div>
                    <div className="agentAuditSecondary">{item.stage} • {shortText(item.detail, compact ? 120 : 180)}</div>
                    {!compact && item.files && item.files.length > 0 ? (
                      <div className="agentAuditSecondary">files: {item.files.join(", ")}</div>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.memoryHits.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Memory used</div>
              <div className="agentAuditList">
                {snapshot.memoryHits.slice(0, compact ? 2 : 4).map((hit, index) => (
                  <div key={`${snapshot.id}-memory-${index}`} className="agentAuditRow">
                    <div className="agentAuditPrimary">{hit.title}</div>
                    <div className="agentAuditSecondary">{hit.kind} • {hit.source}</div>
                    {!compact && hit.text ? <div className="agentAuditSecondary">{shortText(hit.text)}</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.verification && snapshot.verification.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Verifier checks</div>
              <div className="agentAuditList">
                {snapshot.verification.map((check, index) => (
                  <div key={`${snapshot.id}-verify-${index}`} className={`agentAuditRow ${check.ok ? "ok" : "error"}`}>
                    <div className="agentAuditPrimary">{check.name}</div>
                    <div className="agentAuditSecondary">{check.ok ? "ok" : "warn"} • {shortText(check.detail, compact ? 120 : 180)}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.skills.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Skills chosen</div>
              <div className="agentAuditTagRow">
                {snapshot.skills.map((skill) => (
                  <span key={`${snapshot.id}-skill-${skill.skillId}`} className="agentAuditTag">{skill.skillId}</span>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.mcpServers.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">MCP boundary</div>
              <div className="agentAuditList">
                {snapshot.mcpServers.slice(0, compact ? 2 : 4).map((server) => (
                  <div key={`${snapshot.id}-server-${server.name}`} className="agentAuditRow">
                    <div className="agentAuditPrimary">{server.name}</div>
                    <div className="agentAuditSecondary">{server.transport} • {server.target}</div>
                    {server.tools.length > 0 ? <div className="agentAuditSecondary">tools: {server.tools.join(", ")}</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.mcpToolsUsed.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Tool calls executed</div>
              <div className="agentAuditList">
                {snapshot.mcpToolsUsed.map((tool, index) => (
                  <div key={`${snapshot.id}-tool-${index}`} className={`agentAuditRow ${tool.ok ? "ok" : "error"}`}>
                    <div className="agentAuditPrimary">{tool.server}.{tool.tool}</div>
                    <div className="agentAuditSecondary">{tool.ok ? "ok" : "error"} • {tool.durationMs}ms</div>
                    {tool.error ? <div className="agentAuditSecondary">{shortText(tool.error, 180)}</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
};
