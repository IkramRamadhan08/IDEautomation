-- Voice IDE hosted-product trial schema
-- Target: Vercel + serverless API + Supabase as system of record
-- Notes:
-- - Keep runtime/terminal state out of Postgres for now unless it is metadata only.
-- - Favor durable hosted state: users, projects, membership, preferences.
-- - This schema is intentionally small for early product-worth testing.

create extension if not exists pgcrypto;

create table if not exists public.profiles (
  id text primary key,
  supabase_user_id uuid unique,
  email text,
  display_name text,
  avatar_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.projects (
  id uuid primary key default gen_random_uuid(),
  owner_id text not null references public.profiles(id) on delete cascade,
  name text not null,
  slug text not null,
  root text not null,
  description text,
  archived boolean not null default false,
  agent_mode_default text not null default 'hybrid',
  runtime_status text not null default 'idle',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint projects_owner_slug_unique unique (owner_id, slug)
);

create table if not exists public.project_members (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.projects(id) on delete cascade,
  profile_id text not null references public.profiles(id) on delete cascade,
  role text not null default 'owner',
  created_at timestamptz not null default now(),
  unique (project_id, profile_id)
);

create table if not exists public.user_preferences (
  profile_id text primary key references public.profiles(id) on delete cascade,
  llm_provider text,
  build_mode text,
  openai_model text,
  anthropic_model text,
  openrouter_model text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.project_preferences (
  project_id uuid primary key references public.projects(id) on delete cascade,
  build_mode text,
  preview_entry text,
  default_prompt_style text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.user_provider_secrets (
  profile_id text not null references public.profiles(id) on delete cascade,
  provider text not null,
  secret_ciphertext text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (profile_id, provider)
);

create index if not exists idx_projects_owner_id on public.projects(owner_id);
create index if not exists idx_projects_updated_at on public.projects(updated_at desc);
create index if not exists idx_project_members_profile_id on public.project_members(profile_id);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_profiles_updated_at on public.profiles;
create trigger trg_profiles_updated_at
before update on public.profiles
for each row execute function public.set_updated_at();

drop trigger if exists trg_projects_updated_at on public.projects;
create trigger trg_projects_updated_at
before update on public.projects
for each row execute function public.set_updated_at();

drop trigger if exists trg_user_preferences_updated_at on public.user_preferences;
create trigger trg_user_preferences_updated_at
before update on public.user_preferences
for each row execute function public.set_updated_at();

drop trigger if exists trg_project_preferences_updated_at on public.project_preferences;
create trigger trg_project_preferences_updated_at
before update on public.project_preferences
for each row execute function public.set_updated_at();

alter table public.profiles enable row level security;
alter table public.projects enable row level security;
alter table public.project_members enable row level security;
alter table public.user_preferences enable row level security;
alter table public.project_preferences enable row level security;
alter table public.user_provider_secrets enable row level security;

-- Trial-stage RLS:
-- app server may still use service role for API writes,
-- but these policies prepare the schema for direct user-scoped reads/writes too.

do $$ begin
  create policy "profiles_select_self" on public.profiles
    for select using (auth.uid()::text = supabase_user_id::text);
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "profiles_update_self" on public.profiles
    for update using (auth.uid()::text = supabase_user_id::text);
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "projects_select_member" on public.projects
    for select using (
      exists (
        select 1 from public.project_members pm
        join public.profiles p on p.id = pm.profile_id
        where pm.project_id = projects.id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "projects_insert_owner" on public.projects
    for insert with check (
      exists (
        select 1 from public.profiles p
        where p.id = owner_id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "projects_update_member" on public.projects
    for update using (
      exists (
        select 1 from public.project_members pm
        join public.profiles p on p.id = pm.profile_id
        where pm.project_id = projects.id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "project_members_select_member" on public.project_members
    for select using (
      exists (
        select 1 from public.profiles p
        where p.id = profile_id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "user_preferences_select_self" on public.user_preferences
    for select using (
      exists (
        select 1 from public.profiles p
        where p.id = profile_id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "user_preferences_update_self" on public.user_preferences
    for update using (
      exists (
        select 1 from public.profiles p
        where p.id = profile_id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "project_preferences_select_member" on public.project_preferences
    for select using (
      exists (
        select 1 from public.project_members pm
        join public.profiles p on p.id = pm.profile_id
        where pm.project_id = project_preferences.project_id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "user_provider_secrets_select_self" on public.user_provider_secrets
    for select using (
      exists (
        select 1 from public.profiles p
        where p.id = profile_id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "user_provider_secrets_insert_self" on public.user_provider_secrets
    for insert with check (
      exists (
        select 1 from public.profiles p
        where p.id = profile_id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "user_provider_secrets_update_self" on public.user_provider_secrets
    for update using (
      exists (
        select 1 from public.profiles p
        where p.id = profile_id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "user_provider_secrets_delete_self" on public.user_provider_secrets
    for delete using (
      exists (
        select 1 from public.profiles p
        where p.id = profile_id and p.supabase_user_id::text = auth.uid()::text
      )
    );
exception when duplicate_object then null; end $$;
