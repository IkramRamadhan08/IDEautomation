-- Appora durable agent job ledger
-- Safe to run repeatedly. This adds only agent_jobs / agent_job_events
-- and does not drop or rewrite existing project/user tables.

create extension if not exists pgcrypto;

create table if not exists public.agent_jobs (
  id uuid primary key default gen_random_uuid(),
  owner_id text not null references public.profiles(id) on delete cascade,
  project_root text not null default '.',
  build_mode text,
  status text not null default 'queued',
  input text not null default '',
  result jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz
);

create table if not exists public.agent_job_events (
  id bigserial primary key,
  job_id uuid not null references public.agent_jobs(id) on delete cascade,
  owner_id text not null references public.profiles(id) on delete cascade,
  event_type text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_agent_jobs_owner_updated
  on public.agent_jobs(owner_id, updated_at desc);

create index if not exists idx_agent_jobs_owner_project
  on public.agent_jobs(owner_id, project_root, updated_at desc);

create index if not exists idx_agent_job_events_job_id
  on public.agent_job_events(job_id, id);

create index if not exists idx_agent_job_events_owner_job_id
  on public.agent_job_events(owner_id, job_id, id);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_agent_jobs_updated_at on public.agent_jobs;
create trigger trg_agent_jobs_updated_at
before update on public.agent_jobs
for each row execute function public.set_updated_at();

alter table public.agent_jobs enable row level security;
alter table public.agent_job_events enable row level security;

grant select, insert, update on public.agent_jobs to authenticated;
grant select, insert on public.agent_job_events to authenticated;
grant usage, select on sequence public.agent_job_events_id_seq to authenticated;

do $$ begin
  create policy "agent_jobs_select_owner" on public.agent_jobs
    for select using (
      exists (
        select 1 from public.profiles p
        where p.id = owner_id and p.supabase_user_id::text = (select auth.uid())::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "agent_jobs_insert_owner" on public.agent_jobs
    for insert with check (
      exists (
        select 1 from public.profiles p
        where p.id = owner_id and p.supabase_user_id::text = (select auth.uid())::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "agent_jobs_update_owner" on public.agent_jobs
    for update using (
      exists (
        select 1 from public.profiles p
        where p.id = owner_id and p.supabase_user_id::text = (select auth.uid())::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "agent_job_events_select_owner" on public.agent_job_events
    for select using (
      exists (
        select 1 from public.profiles p
        where p.id = owner_id and p.supabase_user_id::text = (select auth.uid())::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "agent_job_events_insert_owner" on public.agent_job_events
    for insert with check (
      exists (
        select 1 from public.profiles p
        where p.id = owner_id and p.supabase_user_id::text = (select auth.uid())::text
      )
    );
exception when duplicate_object then null; end $$;
