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
- No terminal execution in hosted mode.
- No long-running preview server in hosted mode.
- No reliance on process memory for user/project state.
- Binary assets should move to Supabase Storage before production use.

## UX Separation

- Agent orb: persona, animation, and streamed conversational response.
- Interaction module: file writes, tools, validation, preview audit, memory/skill/MCP trace.
- Settings: provider choice must explain free/cheap vs paid expectations clearly.
