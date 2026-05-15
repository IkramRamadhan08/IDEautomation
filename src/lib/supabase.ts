import { createClient } from "@supabase/supabase-js";

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL || "";
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY || "";

export const supabase = createClient(supabaseUrl, supabaseAnonKey);

function findAccessToken(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const direct = typeof record.access_token === "string" ? record.access_token.trim() : "";
  if (direct) return direct;
  for (const key of ["currentSession", "session"]) {
    const nested = findAccessToken(record[key]);
    if (nested) return nested;
  }
  return null;
}

export function getCachedSupabaseAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index) || "";
      if (!key.startsWith("sb-") || !key.endsWith("-auth-token")) continue;
      const raw = window.localStorage.getItem(key);
      if (!raw) continue;
      const token = findAccessToken(JSON.parse(raw));
      if (token) return token;
    }
  } catch {
    return null;
  }
  return null;
}
