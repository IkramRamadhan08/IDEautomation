import { type BuildMode } from "../types";
import { claraProfile, getClaraQuickPrompts } from "./clara";
import { rakaProfile, getRakaQuickPrompts } from "./raka";
import { type AgentQuickPrompt, type BuildModeProfile } from "./modeTypes";

const PROFILES: Record<BuildMode, BuildModeProfile> = {
  "full-agent": claraProfile,
  hybrid: rakaProfile,
};

export function getBuildModeProfile(mode: BuildMode): BuildModeProfile {
  return PROFILES[mode] ?? PROFILES.hybrid;
}

export function getModeQuickPrompts(
  mode: BuildMode,
  options: { activeFile: string; previewUrl: string }
): AgentQuickPrompt[] {
  if (mode === "full-agent") {
    return getClaraQuickPrompts(options.previewUrl);
  }

  return getRakaQuickPrompts(options.activeFile, options.previewUrl);
}
