# Appora Docs

Docs are grouped by ownership so maintenance work has an obvious starting point.

```text
architecture/   Runtime, hosted-mode, and agent architecture notes
handoffs/       Handoff notes for future AI agents
reports/        Benchmark and audit reports
supabase/       Supabase schema and migration SQL
superpowers/    Implementation plans used during agentic development
```

## Active References

- Main hosted schema: `supabase/schema.sql`
- Supabase RAG table migration: `supabase/agent-rag.sql`
- Supabase durable agent jobs migration: `supabase/agent-jobs.sql`
- Hosted runtime design: `architecture/hosted-agent-scheme.md`
- Agent architecture: `architecture/agent-architecture.md`
- Latest handoff: `handoffs/agent-handoff-2026-06-10.md`

Keep docs paths in API warning strings and README examples synchronized when moving SQL files.
