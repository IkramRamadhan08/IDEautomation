import { type BuildMode } from "../types";

export type AgentQuickPrompt = {
  label: string;
  prompt?: string;
  action?: "start-preview";
};

export type AgentRunPlan = {
  requestEditorStatus: string;
  shouldDrivePreview: boolean;
  shouldRunValidation: boolean;
  shouldAuditPreview: boolean;
};

export type BuildModeProfile = {
  mode: BuildMode;
  label: string;
  personaName: string;
  personaRole: string;
  topbarSubtitle: string;
  settingsDescription: string;
  modeSummary: string;
  requestEditorStatus: string;
  idleLines: string[];
  playfulLines: string[];
  curiousLines: string[];
  sleepyLines: string[];
  sleepingLines: string[];
  celebrateLines: string[];
  surprisedLines: string[];
  errorLines: string[];
};
