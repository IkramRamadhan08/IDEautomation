# Appora

Appora is an experimental agentic web/app builder for non-coders and fast-moving builders. It combines a hosted browser IDE, Supabase-backed project persistence, BYOK model routing, and one coding agent designed to help users move from rough intent to a working web application.

This repository is moving toward a serious coding-agent product, but it is not yet honestly comparable to mature agents such as Codex, Cursor, Claude Code, or Aider on large arbitrary codebases. The current system has a real tool backbone, validation loop, preview audit, memory, and guarded execution, but long-horizon reliability and benchmark performance still need hardening.

The target runtime is **Vercel serverless + Supabase**. The product is not positioned as a local-only experiment or short-lived showcase; the architecture is meant for a hosted experience where users sign in, paste their own model API keys, create projects, and ask an agent to build or improve apps.

## Product Direction

Appora is built around one agent with two workspace layouts:

- **Workspace**: the primary IDE layout with file explorer, Monaco editor, preview, settings, and the agent orb. The agent stays close to the active file and project context when the user is working directly.
- **Full Preview**: the same agent and runtime shown in a preview-first layout. This is not a second agent; it only gives the running app more screen space while the agent keeps the same tools, memory, validation, and repair loop.

The intended user is someone who may not know how to code but wants to build a real web/app surface by chatting with an agent, reviewing the result, and iterating.

## Core Capabilities

- Hosted auth and project persistence with Supabase
- Browser-based project workspace and file explorer
- Lightweight nvim-style workspace editor surface
- Full Preview layout for larger app review
- Floating agent orb for conversational streaming
- Separate live interaction module for agent actions only
- Supabase-backed user settings and project files
- BYOK provider settings per user
- 9Router-backed model routing with BYOK support
- Free-tier friendly mode for providers with strict limits
- Agent memory and RAG-ready Supabase document chunks
- Durable agent job ledger with `job_id`, event history, and recoverable final result
- Checkpoint and restore before agent file writes
- Serverless-compatible terminal command execution surface
- Project validation and preview audit hooks
- Real local edit tools for line-range and search/replace file edits
- Formatter/linter tool wrapper for project files
- Local SQLite database query/migration tool
- Guarded local Git branch/commit tool
- Aider benchmark adapter for measuring Appora against coding-agent tasks

## Current Maturity

Appora is best described as an early serious coding agent, not a finished top-tier one.

Rough current confidence:

- Tool backbone: about 75-80%
- Agent flow/orchestration: about 65-75%
- Frontend/web app delivery: about 65-75%
- General coding across varied repos: about 60-70%
- Benchmark readiness: about 60-70%

What is already solid:

- The runtime inspects project structure before broad work.
- The agent can read/search/map files, apply focused edits, run bounded validation, run formatter/linter checks, query local SQLite, and inspect/commit local Git changes.
- Frontend tasks are blocked by source-quality, requirement-coverage, task-depth, interaction, business-data honesty, and preview-audit checks.
- Browser visual evidence is part of the completion report when preview audit runs.
- Agent runs persist state, events, memory, checkpoints, validation results, preview audit results, repairs, and completion reports.

What is still not solved:

- Supabase/Postgres app database operations are not yet a first-class agent tool. SQLite is supported locally; Supabase/Postgres should be wired through MCP or a dedicated guarded backend client.
- GitHub push and PR automation are intentionally gated. Local branch/commit is supported; remote push/PR requires explicit remote permission and a configured GitHub workflow.
- Dependency/version conflict resolution is still basic. The agent can inspect and run package managers, but it is not yet a full dependency solver.
- The model/tool loop can still underuse tools or produce shallow work if the model output is weak, although the verifier now blocks more generic fallback cases.
- Aider benchmark support exists, but Appora does not yet claim competitive pass rates.
- Hosted serverless execution is bounded; this is not a persistent VM or fully isolated cloud sandbox.

## Agent Runtime

The backend agent runtime uses Appora's own **linear runtime v2**. The current pipeline is:

```text
intent
  -> memory
  -> skills
  -> mcp
  -> plan
  -> deep_preflight
  -> read_only_scout (complex tasks only)
  -> draft
  -> tooling / refine
  -> verify
  -> finalize
```

Important runtime behavior:

- Intent detection keeps greetings and normal chat read-only.
- `AgentRunController` bounds driver steps, LLM calls, and tool calls so long tasks stop with a clear reason instead of looping indefinitely.
- `run_ledger` records compact phase-level evidence for every run.
- Runtime hooks record internal lifecycle events for phase entry, tools, scout, budget stop, and finalize without executing user hook commands in this slice.
- Deep work preflight automatically inspects larger tasks before drafting.
- Read-only scout runs project-scoped repo analysis for complex tasks without letting parallel workers write files.
- Local read-only tools give the model structured repo context.
- MCP tools can be discovered and executed through registered configs.
- Verifier checks block unsafe or invalid output before files are applied.
- Verifier repair pass gives the agent one more chance to correct bad output.
- Checkpoints are written before applying file changes, so the latest agent write can be restored.

## Local Agent Tools

The runtime exposes project-scoped local tools to the model:

- `repo_list`: list project files without dependency/build noise
- `repo_read`: read one file
- `repo_read_many`: read multiple files in one bounded call
- `repo_search`: search source files
- `repo_map`: build an aider-style repo map
- `file_window`: inspect a line-numbered file window
- `symbol_search`: find functions/classes/components/types
- `style_stack`: inspect styling conventions
- `line_replace_preview` / `line_replace_apply`: preview or apply line-range edits
- `search_replace_preview` / `search_replace_apply`: preview or apply search/replace edits
- `package_scripts`: inspect scripts, dependencies, and package manager hints
- `repo_overview`: summarize project shape and key files
- `stack_profile`: detect languages, frameworks, database, infra, and preview signals
- `validation_plan`: suggest stack-specific validation commands
- `test_runner`: run bounded validation commands
- `format_lint`: run bounded formatter/linter checks or fixes
- `database_client`: query/migrate/schema local SQLite databases
- `git_manager`: inspect Git and perform guarded local branch/commit operations
- `docs_browser`: fetch bounded public HTTPS documentation text
- `skill_catalog` / `skill_read`: discover local/imported skills
- `dependency_graph`: build a bounded JS/TS import graph
- `component_index`: index React components and hooks
- `route_map`: inspect likely app routes and navigation
- `quality_scan`: scan production-readiness risks
- `memory_overview`: inspect memory backend readiness
- `mcp_status`: inspect configured MCP servers
- `preview_capabilities`: inspect preview surfaces

These tools are designed to reduce guessing and make the Appora agent behave more like a real coding agent.

Some tools are intentionally guarded. Remote Git push/PR and production database operations should not run silently. The current default is local-first safety.

## Stack

- Frontend: React 19, Vite, TypeScript
- Editor: lightweight nvim-style textarea surface
- UI primitives: Radix UI, lucide-react, framer-motion
- Backend: FastAPI
- Agent runtime: Appora linear runtime v2
- Auth and persistence: Supabase
- Deploy target: Vercel serverless

## Repository Layout

```text
api/                    FastAPI backend and agent runtime
api/tests/              Backend regression tests
docs/                   Supabase/RAG docs and SQL
scripts/                Utility scripts and preview audit
src/                    React frontend
src/agent/              Frontend agent workflow/runtime helpers
src/components/         UI components
src/modes/              Workspace and Full Preview layouts
SUPABASE_SCHEMA.sql     Main Supabase schema
vercel.json             Vercel routing/build config
```

## Local Development

Install frontend dependencies:

```bash
npm install
```

Create and install the backend environment:

```bash
python3 -m venv api/.venv
source api/.venv/bin/activate
pip install -e ./api
```

Run the backend:

```bash
source api/.venv/bin/activate
uvicorn api.main:app --reload --host 0.0.0.0 --port 8787
```

Run the frontend:

```bash
npm run dev
```

By default the local frontend calls the local API at `http://localhost:8787`.
For local-first development while Railway or another hosted backend is unavailable, run:

```bash
npm run dev:local
```

Then open `http://localhost:5173`. Do not use the Vercel-hosted UI for local backend work unless the local API is exposed through an HTTPS tunnel and `VITE_API_BASE` points to that tunnel.

## Supabase Setup

Create a Supabase project and run:

- `SUPABASE_SCHEMA.sql`
- `docs/supabase-agent-rag.sql`

The RAG SQL creates `public.agent_memory_chunks`, used by the agent memory backend when available.

Useful backend readiness endpoints:

- `GET /api/supabase/rag/status?project_root=.`
- `POST /api/supabase/rag/sync` with `{ "project_root": "." }`

If RAG status is `missing`, Supabase is connected but the agent memory table has not been created yet.

`SUPABASE_SCHEMA.sql` also creates:

- `public.agent_jobs`
- `public.agent_job_events`

These tables make agent runs recoverable in hosted mode. `/api/agent` returns/streams a `job_id`, and the frontend can later read job status/events through `/api/agent/jobs/{job_id}` and `/api/agent/jobs/{job_id}/events`.

If your Supabase project already has the earlier Appora schema, run only `docs/supabase-agent-jobs.sql` to add the durable job ledger without touching existing project tables.

## Hosted Deployment on Vercel

Import the repo into Vercel and use the Vite framework preset. The repo includes `vercel.json` and `api/index.py`, so frontend and API routes are prepared for serverless deployment.

Required environment variables:

```env
VITE_SUPABASE_URL=...
VITE_SUPABASE_ANON_KEY=...
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
VOICEIDE_SECRET_KEY=...
```

`VOICEIDE_SECRET_KEY` is required for hosted BYOK provider secret encryption.

Recommended defaults:

```env
LLM_PROVIDER=nine_router
BUILD_MODE=hybrid
FRIENDLY_FREE_TIER_MODE=true
AGENT_REFINEMENT_MODE=auto
AGENT_MIN_GAP_SECONDS=4
AGENT_REQUESTS_PER_MINUTE=8
AGENT_CONTEXT_CHAR_BUDGET=48000
```

Recommended 9Router defaults:

```env
NINE_ROUTER_BASE_URL=http://127.0.0.1:20128/v1
NINE_ROUTER_MODEL=appora
```

Optional legacy direct-provider model defaults:

```env
OPENAI_MODEL=gpt-5.5
ANTHROPIC_MODEL=claude-opus-4-7
OPENROUTER_MODEL=x-ai/grok-4.3
GROQ_MODEL=groq/compound
GEMINI_MODEL=gemini-3-pro-preview
TOGETHER_MODEL=deepseek-ai/DeepSeek-V4-Pro
CEREBRAS_MODEL=zai-glm-4.7
XAI_MODEL=grok-4.3
```

Optional OAuth settings:

```env
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
```

Optional server-level provider keys:

```env
NINE_ROUTER_API_KEY=...
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
OPENROUTER_API_KEY=...
GROQ_API_KEY=...
GEMINI_API_KEY=...
TOGETHER_API_KEY=...
CEREBRAS_API_KEY=...
XAI_API_KEY=...
```

For hosted public usage, prefer per-user BYOK through Settings instead of sharing one server-level key across all users.

## BYOK Provider Model

Appora is designed for bring-your-own-key usage. The current agent path is centered on 9Router, so users can paste a 9Router endpoint/key in Settings and let 9Router handle the underlying provider routes. Keys are stored per account in Supabase and encrypted using `VOICEIDE_SECRET_KEY`.

For hosted public usage, prefer per-user 9Router BYOK through Settings instead of sharing one server-level key across all users. Direct provider settings may still appear in older code paths and catalog data, but the intended agent runtime is one Appora agent routed through 9Router.

## Validation

Common checks:

```bash
npm run lint
npm run build
npm run test:agent-regression
```

Backend targeted tests can also be run with:

```bash
api/.venv/bin/python -m unittest api.tests.test_agent_regressions.AgentToolsRegressionTests
```

Agent benchmark helpers:

```bash
npm run bench:agent:aider:setup
npm run bench:agent:aider:smoke
npm run bench:agent:internal:live:smoke
```

Benchmark results should be treated as evidence, not marketing copy. Appora should not be described as benchmark-competitive until repeatable pass rates prove it.

## Current Engineering Boundaries

Appora is built for a hosted serverless app-builder workflow. The current architecture is intentionally not a heavy self-hosted container platform.

Known boundaries:

- Vercel serverless is not a persistent VM. Appora configures the Python API function for a 300s max duration so streaming agent work and short validation commands have room to finish, but long-running dev servers still do not belong inside the function.
- Heavy sandbox isolation for arbitrary user workloads is not implemented as a separate container layer.
- Browser preview and terminal behavior depend on the deployment/runtime constraints. Hosted terminal actions are request-scoped and best-effort; local/dev preview servers are disabled on Vercel.
- Provider quality and rate limits depend on each user key and chosen model.

The product direction is to keep improving agent reliability through stronger tools, stricter verification, better project persistence, and clearer hosted UX rather than expanding into a full custom cloud IDE infrastructure.

## Vision

The goal is for the Appora agent to become a competitive coding agent for web/app building:

- In Workspace layout, the agent should feel like a sharp coding partner inside the editor.
- In Full Preview layout, the same agent should take broad product intent and ship a coherent preview-ready version.
- The system should inspect before editing, validate before claiming success, and protect user work with checkpoints.
- Non-coders should be able to create and iterate on real web apps by talking to the agent, not by learning the toolchain first.
