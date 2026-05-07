create table if not exists public.agent_memory_chunks (
  chunk_id text primary key,
  owner_id text not null references public.profiles(id) on delete cascade,
  project_root text not null,
  source_path text not null,
  title text not null,
  content text not null,
  chunk_index integer not null default 0,
  chunk_count integer not null default 1,
  content_hash text not null,
  updated_at timestamptz not null default now()
);

alter table public.agent_memory_chunks
  add column if not exists owner_id text references public.profiles(id) on delete cascade;

create index if not exists agent_memory_chunks_project_root_idx
  on public.agent_memory_chunks (owner_id, project_root, updated_at desc);

create index if not exists agent_memory_chunks_source_path_idx
  on public.agent_memory_chunks (source_path, updated_at desc);

create index if not exists agent_memory_chunks_project_source_idx
  on public.agent_memory_chunks (owner_id, project_root, source_path, chunk_index);

alter table public.agent_memory_chunks enable row level security;

do $$ begin
  create policy "agent_memory_chunks_select_owner" on public.agent_memory_chunks
    for select using (
      exists (
        select 1 from public.profiles p
        where p.id = owner_id and p.supabase_user_id::text = (select auth.uid())::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "agent_memory_chunks_insert_owner" on public.agent_memory_chunks
    for insert with check (
      exists (
        select 1 from public.profiles p
        where p.id = owner_id and p.supabase_user_id::text = (select auth.uid())::text
      )
    );
exception when duplicate_object then null; end $$;

do $$ begin
  create policy "agent_memory_chunks_update_owner" on public.agent_memory_chunks
    for update using (
      exists (
        select 1 from public.profiles p
        where p.id = owner_id and p.supabase_user_id::text = (select auth.uid())::text
      )
    );
exception when duplicate_object then null; end $$;
