import type { AgentAction, AgentLiveItem } from "../types";

const NON_ACTION_PREFIXES = [
  "intent kebaca",
  "backend intent",
  "run trace",
  "memory yang kepake",
  "skill yang dipilih",
  "capability check",
  "free-tier guard",
  "agent run ini punya warning",
];

export function isOperationalLiveItem(item: AgentLiveItem) {
  if (item.role !== "tool") return false;
  const text = item.text.trim().toLowerCase();
  if (!text) return false;
  if (NON_ACTION_PREFIXES.some((prefix) => text.startsWith(prefix))) return false;
  return item.tone === "working" || item.tone === "success" || item.tone === "error";
}

export function actionDetail(action: AgentAction) {
  return String(action.command || action.path || action.tool || action.server || "No extra detail");
}
