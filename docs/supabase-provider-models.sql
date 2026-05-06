alter table if exists public.user_settings
  add column if not exists groq_model text,
  add column if not exists gemini_model text,
  add column if not exists together_model text,
  add column if not exists cerebras_model text,
  add column if not exists xai_model text;
