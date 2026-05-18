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
                {`passes=${snapshot.passes}`} • {snapshot.finalConfidence ? `confidence=${snapshot.finalConfidence} • ` : ""}{`plan=${snapshot.plan?.length || 0}`} • {`verify=${snapshot.verification?.length || 0}`} • {`memory=${snapshot.memoryHits.length}`} • {`skills=${snapshot.skills.length}`} • {`mcp used=${snapshot.mcpToolsUsed.length}`}
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

          {snapshot.contextFiles && snapshot.contextFiles.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Context files</div>
              <div className="agentAuditTagRow">
                {snapshot.contextFiles.slice(0, compact ? 6 : 14).map((file) => (
                  <span key={`${snapshot.id}-context-${file}`} className="agentAuditTag">{file}</span>
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

          {snapshot.validationRuns && snapshot.validationRuns.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Validation runs</div>
              <div className="agentAuditList">
                {snapshot.validationRuns.map((run, index) => (
                  <div key={`${snapshot.id}-validation-${index}`} className={`agentAuditRow ${run.ok ? "ok" : "error"}`}>
                    <div className="agentAuditPrimary">{run.label}</div>
                    <div className="agentAuditSecondary">{run.ok ? "ok" : "failed"} • ran={run.ran} • failed={run.failed}</div>
                    {!compact && run.commands.length > 0 ? <div className="agentAuditSecondary">commands: {run.commands.join(" • ")}</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.appliedPatches && snapshot.appliedPatches.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Applied patches</div>
              <div className="agentAuditList">
                {snapshot.appliedPatches.map((patch, index) => (
                  <div key={`${snapshot.id}-applied-${index}`} className="agentAuditRow ok">
                    <div className="agentAuditPrimary">{patch.label}</div>
                    <div className="agentAuditSecondary">{patch.count} file(s) • {patch.paths.slice(0, compact ? 3 : 8).join(" • ")}</div>
                    {!compact && patch.checkpointPath ? <div className="agentAuditSecondary">checkpoint: {patch.checkpointPath}</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.shellRuns && snapshot.shellRuns.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Shell runs</div>
              <div className="agentAuditList">
                {snapshot.shellRuns.map((run, index) => (
                  <div key={`${snapshot.id}-shell-${index}`} className={`agentAuditRow ${run.ok ? "ok" : "error"}`}>
                    <div className="agentAuditPrimary">{run.command}</div>
                    <div className="agentAuditSecondary">{run.ok ? "ok" : "failed"} • exit={run.returncode ?? "unknown"}</div>
                    {!compact && run.stderrPreview ? <div className="agentAuditSecondary">{shortText(run.stderrPreview, 180)}</div> : null}
                    {!compact && !run.stderrPreview && run.stdoutPreview ? <div className="agentAuditSecondary">{shortText(run.stdoutPreview, 180)}</div> : null}
                    {!compact && run.error ? <div className="agentAuditSecondary">{shortText(run.error, 180)}</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.previewAudits && snapshot.previewAudits.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Preview audits</div>
              <div className="agentAuditList">
                {snapshot.previewAudits.map((audit, index) => (
                  <div key={`${snapshot.id}-preview-${index}`} className={`agentAuditRow ${audit.blocking === 0 ? "ok" : "error"}`}>
                    <div className="agentAuditPrimary">{audit.label}</div>
                    <div className="agentAuditSecondary">{audit.auditMode} • blocking={audit.blocking} • warnings={audit.warnings}</div>
                    {!compact ? <div className="agentAuditSecondary">{shortText(audit.summary, 180)}</div> : null}
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.repairPasses && snapshot.repairPasses.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Repair passes</div>
              <div className="agentAuditList">
                {snapshot.repairPasses.map((repair, index) => (
                  <div key={`${snapshot.id}-repair-${index}`} className={`agentAuditRow ${repair.verifierFailures === 0 ? "ok" : "error"}`}>
                    <div className="agentAuditPrimary">{repair.label}</div>
                    <div className="agentAuditSecondary">changes={repair.producedChanges} • actions={repair.producedActions} • verifier failures={repair.verifierFailures}</div>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {snapshot.commandPolicyDecisions && snapshot.commandPolicyDecisions.length > 0 ? (
            <div className="agentAuditSection">
              <div className="agentAuditSectionTitle">Command policy</div>
              <div className="agentAuditList">
                {snapshot.commandPolicyDecisions.map((decision, index) => (
                  <div key={`${snapshot.id}-policy-${index}`} className={`agentAuditRow ${decision.ok ? "ok" : "error"}`}>
                    <div className="agentAuditPrimary">{decision.command}</div>
                    <div className="agentAuditSecondary">{decision.riskLevel} • {shortText(decision.reason, compact ? 120 : 180)}</div>
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
