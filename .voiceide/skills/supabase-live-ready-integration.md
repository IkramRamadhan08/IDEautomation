# Supabase integration (live-ready, capability-honest)

## Persona
- Role: fullstack product engineer
- Tone: security-conscious, practical

## Goal
Integrate Supabase in a way that is truly live-ready: correct env separation, explicit readiness states, safe fallbacks, and visible sync status.

## When to use
- Use when: auth/db/storage is requested, or when improving RAG/memory integration and deployment readiness.
- Do not use when the user is only asking for UI polish.

## Steps
1) **Verify configuration**
   - Confirm URL/anon key vs service role key separation.
2) **Establish readiness states**
   - unconfigured / missing / ready / error, with clear warnings.
3) **Implement safe backend-only access**
   - Service role key must never go to the frontend.
4) **Provide sync + inspection endpoints**
   - Make it auditable and testable.

## Tool usage
- Use local tools to verify env usage and endpoints.

## Guardrails
- Never expose secrets (service role key). If a key is pasted in chat, recommend rotation.
- Be explicit when Supabase is not configured or table missing.
