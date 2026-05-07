# Hosted Agent Scheme

Target product: a Vercel serverless web builder for non-coders, with Supabase as durable storage.

## Product Contract

- Users should describe outcomes in plain language.
- Clara owns end-to-end product building and should make sensible product decisions from vague briefs.
- Raka is the scoped copilot for focused edits, review, and explanation.
- The app must not depend on a local terminal, local folders, or long-running server state in hosted mode.
- Text project files are persisted in Supabase and hydrated into `/tmp` only as a request-time working cache.

## Agent Loop

1. Classify intent: conversation, inspection, mixed, or command.
2. For conversation, stream a normal reply and keep file changes/actions empty.
3. For inspection, read context and report findings without edits unless the user explicitly asks to fix.
4. For command, gather active/open/related files, memory, skills, and tool context.
5. Generate full-file edits, not patches.
6. Apply edits through the file API so Supabase persistence runs.
7. Validate when possible and run one repair pass if validation/audit finds issues.
8. Report the outcome in plain language.

## Provider Strategy

Provider access is BYOK. The hosted product should guide non-coders toward the least-friction provider:

- OpenRouter: recommended default for trial/free users. Prefer `openrouter/free`, `:free`, or cheap models first.
- OpenAI: familiar path for non-coders. It can use trial/account credits when available, but should be described as token-billed rather than unlimited free-tier.
- Anthropic: careful edit path for users with API credits.
- Groq: fast OpenAI-compatible free-plan path for users who want paste-key-and-build behavior with rate limits.
- Gemini: Google AI Studio path for users who already have Google/Gemini keys and free quota.
- Together AI: broad open-source model catalog through an OpenAI-compatible API.
- Cerebras: smaller catalog, very fast inference for supported open models.
- xAI: Grok model path for users with xAI API access.

The wrapper should:

- return friendly quota/billing/rate-limit errors,
- avoid long sleep/retry loops in Vercel serverless,
- expose recommended/free-tier model options in Settings,
- store keys per Supabase profile with `VOICEIDE_SECRET_KEY`.

## Serverless Boundaries

- No arbitrary host folder selection in hosted mode.
- Terminal execution in hosted mode is best-effort request-scoped execution only. It can run short project commands, but it must not be treated like a persistent VM terminal.
- No long-running preview server in hosted mode.
- No reliance on process memory for user/project state.
- Binary assets should move to Supabase Storage before production use.

## Vercel Function Runtime

All API routes are routed through `api/index.py` as one Python Vercel Function. The function is configured with `maxDuration: 300` so agent streaming, validation, Supabase hydration, and short terminal actions have enough time to finish on Hobby/Fluid Compute defaults.

Runtime expectations:

- Agent chat/build requests should stream SSE from `/api/agent` so users see progress before the function finishes.
- Every `/api/agent` run now gets a `job_id`. When Supabase has the `agent_jobs` and `agent_job_events` tables from `SUPABASE_SCHEMA.sql`, status/events/final result are persisted while the stream runs.
- `/api/agent` also supports `background: true` for queue-only submission. The full request payload is stored in `agent_jobs.request_payload`, then `/api/agent/worker/run` can resume the job later.
- `vercel.json` schedules `/api/agent/worker/run?limit=1` every five minutes. Set `CRON_SECRET` or `AGENT_WORKER_SECRET` in Vercel so the worker endpoint is authorized in hosted deployments.
- Existing Supabase projects can apply only `docs/supabase-agent-jobs.sql` to add those two tables without rerunning the full schema.
- Clients can poll `GET /api/agent/jobs/{job_id}` and `GET /api/agent/jobs/{job_id}/events` to recover progress/result after a dropped browser connection.
- Agent jobs still need to stay bounded; the worker is a durable resume lane for queued jobs, not an unlimited daemon.
- `/api/run/start` stays disabled on Vercel because a dev server is a long-running process.
- `/api/terminal/run` should stay scoped to short commands. Long-running commands, watch modes, and background daemons are not compatible with serverless.
- Durable state belongs in Supabase. `/tmp` is only a per-invocation working cache and can disappear between requests.

## UX Separation

- Agent orb: persona, animation, and streamed conversational response.
- Interaction module: file writes, tools, validation, preview audit, memory/skill/MCP trace.
- Settings: provider choice must explain free/cheap vs paid expectations clearly.
