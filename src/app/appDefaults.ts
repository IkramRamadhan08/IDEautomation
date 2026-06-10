import type { GoogleAuthStatus, ProviderChoice, SettingsInfo } from "../shared/types";

export type AppTheme = "light" | "dark";

export function getDefaultAssistPaneWidth() {
  if (typeof window === "undefined") return 280;
  return Math.max(220, Math.min(280, Math.floor(window.innerWidth * 0.24)));
}

export function localDevUser(): GoogleAuthStatus {
  return {
    ok: true,
    authenticated: true,
    phase: "local-dev",
    user: {
      sub: "local-dev-user",
      email: "local@appora.dev",
      name: "Local Appora Tester",
      picture: null,
    },
  };
}

export function modelFromSettings(provider: ProviderChoice, source: SettingsInfo | null): string {
  if (!source) return "";
  if (provider === "nine_router") return source.nine_router_model || "free-forever";
  if (provider === "openai") return source.openai_model || "";
  if (provider === "anthropic") return source.anthropic_model || "";
  if (provider === "openrouter") return source.openrouter_model || "";
  if (provider === "groq") return source.groq_model || "";
  if (provider === "gemini") return source.gemini_model || "";
  if (provider === "together") return source.together_model || "";
  if (provider === "cerebras") return source.cerebras_model || "";
  if (provider === "xai") return source.xai_model || "";
  return "";
}
