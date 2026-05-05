create table if not exists public.agent_memory_chunks (
  chunk_id text primary key,
  project_root text not null,
  source_path text not null,
  title text not null,
  content text not null,
  chunk_index integer not null default 0,
  chunk_count integer not null default 1,
  content_hash text not null,
  updated_at timestamptz not null default now()
);

create index if not exists agent_memory_chunks_project_root_idx
  on public.agent_memory_chunks (project_root, updated_at desc);

create index if not exists agent_memory_chunks_source_path_idx
  on public.agent_memory_chunks (source_path, updated_at desc);
