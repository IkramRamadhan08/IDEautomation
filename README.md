# Voice IDE

Voice IDE is an agentic web/app builder for non-coders and fast-moving builders. It combines a hosted browser IDE, Supabase-backed project persistence, BYOK model providers, and two coding agents designed to help users move from rough intent to a working web application.

The target runtime is **Vercel serverless + Supabase**. The product is not positioned as a local-only experiment or short-lived showcase; the architecture is meant for a hosted experience where users sign in, paste their own model API keys, create projects, and ask an agent to build or improve apps.

## Product Direction

Voice IDE is built around two agents:

- **Clara**: full-agent mode. Clara is the autonomous product builder that can take a rough brief, inspect the repo, plan the implementation, edit files, validate, repair, and push toward a coherent preview-ready result.
- **Raka**: hybrid mode. Raka is the IDE copilot that stays close to the active file, editor state, and current project context for precise assisted coding.

The intended user is someone who may not know how to code but wants to build a real web/app surface by chatting with an agent, reviewing the result, and iterating.

## Core Capabilities

- Hosted auth and project persistence with Supabase
- Browser-based project workspace and file explorer
- Monaco-powered hybrid IDE surface
- Full-agent autonomous build mode
- Floating agent orb for conversational streaming
- Separate live interaction module for agent actions only
- Supabase-backed user settings and project files
- BYOK provider settings per user
- Multi-provider model support:
  - OpenAI
  - Anthropic
  - OpenRouter
  - Groq
  - Gemini
  - Together AI
  - Cerebras
  - xAI
- Free-tier friendly mode for providers with strict limits
- Agent memory and RAG-ready Supabase document chunks
- Checkpoint and restore before agent file writes
- Serverless-compatible terminal command execution surface
- Project validation and preview audit hooks

## Agent Runtime

The backend agent runtime uses **LangGraph**. The current graph is:

```text
intent
  -> memory
  -> skills
  -> mcp
  -> plan
  -> deep_preflight
  -> draft
  -> tooling / refine
  -> verify
  -> finalize
```

Important runtime behavior:

- Intent detection keeps greetings and normal chat read-only.
- Deep work preflight automatically inspects larger tasks before drafting.
- Local read-only tools give the model structured repo context.
- MCP tools can be discovered and executed through registered configs.
- Verifier checks block unsafe or invalid output before files are applied.
- Verifier repair pass gives the agent one more chance to correct bad output.
- Checkpoints are written before applying file changes, so the latest agent write can be restored.

## Local Agent Tools

The runtime exposes read-only local tools to the model:

- `repo_list`: list project files without dependency/build noise
- `repo_read`: read one file
- `repo_read_many`: read multiple files in one bounded call
- `repo_search`: search source files
- `package_scripts`: inspect scripts, dependencies, and package manager hints
- `repo_overview`: summarize project shape and key files
- `dependency_graph`: build a bounded JS/TS import graph

These tools are designed to reduce guessing and make Clara/Raka behave more like real coding agents.

## Stack

- Frontend: React 19, Vite, TypeScript
- Editor: Monaco
- UI primitives: Radix UI, lucide-react, framer-motion
- Backend: FastAPI
- Agent graph: LangGraph
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
src/modes/              Full-agent and hybrid workspaces
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

By default the Vite frontend calls the local API at `http://localhost:8787` in development.

## Supabase Setup

Create a Supabase project and run:

- `SUPABASE_SCHEMA.sql`
- `docs/supabase-agent-rag.sql`

The RAG SQL creates `public.agent_memory_chunks`, used by the agent memory backend when available.

Useful backend readiness endpoints:

- `GET /api/supabase/rag/status?project_root=.`
- `POST /api/supabase/rag/sync` with `{ "project_root": "." }`

If RAG status is `missing`, Supabase is connected but the agent memory table has not been created yet.

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
LLM_PROVIDER=openrouter
BUILD_MODE=hybrid
FRIENDLY_FREE_TIER_MODE=true
AGENT_REFINEMENT_MODE=auto
AGENT_MIN_GAP_SECONDS=4
AGENT_REQUESTS_PER_MINUTE=8
AGENT_CONTEXT_CHAR_BUDGET=48000
```

Optional default model settings:

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

Voice IDE is designed for bring-your-own-key usage. Users can paste provider API keys in Settings. Keys are stored per account in Supabase and encrypted using `VOICEIDE_SECRET_KEY`.

OpenRouter is a good default provider for public hosted deployments because it gives users one router key and access to free or lower-cost models. Groq, Gemini, and Cerebras are useful for users who want free/dev-tier experimentation with stricter limits. OpenAI remains available because it is familiar, but OpenAI API usage is credit/billing based rather than unlimited free tier.

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

## Current Engineering Boundaries

Voice IDE is built for a hosted serverless app-builder workflow. The current architecture is intentionally not a heavy self-hosted container platform.

Known boundaries:

- Vercel serverless is not a long-running compute runtime.
- Heavy sandbox isolation for arbitrary user workloads is not implemented as a separate container layer.
- Browser preview and terminal behavior depend on the deployment/runtime constraints.
- Provider quality and rate limits depend on each user key and chosen model.

The product direction is to keep improving agent reliability through stronger tools, stricter verification, better project persistence, and clearer hosted UX rather than expanding into a full custom cloud IDE infrastructure.

## Vision

The goal is for Clara and Raka to become competitive coding agents for web/app building:

- Clara should be able to take broad product intent and ship a coherent first version.
- Raka should make the hybrid IDE feel like working with a sharp coding partner.
- The system should inspect before editing, validate before claiming success, and protect user work with checkpoints.
- Non-coders should be able to create and iterate on real web apps by talking to the agent, not by learning the toolchain first.
