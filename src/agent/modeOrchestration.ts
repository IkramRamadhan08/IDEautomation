import { type BuildMode } from "../types";
import { buildClaraRepairPrompt, buildClaraRunPlan } from "./clara";
import { buildRakaRepairPrompt, buildRakaRunPlan } from "./raka";
export type { AgentRunPlan } from "./modeTypes";

export function buildAgentRunPlan(buildMode: BuildMode, input: string, previewUrl: string) {
  return buildMode === "full-agent"
    ? buildClaraRunPlan(input)
    : buildRakaRunPlan(input, previewUrl);
}

export function buildRepairPrompt(
  buildMode: BuildMode,
  originalInput: string,
  validationReport?: string | null,
  previewAuditReport?: string | null,
): string {
  return buildMode === "full-agent"
    ? buildClaraRepairPrompt(originalInput, validationReport, previewAuditReport)
    : buildRakaRepairPrompt(originalInput, validationReport, previewAuditReport);
}
