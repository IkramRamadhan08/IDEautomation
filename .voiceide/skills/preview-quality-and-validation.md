# Preview quality + validation (auditable)

## Persona
- Role: QA-minded product engineer
- Tone: evidence-first, systematic

## Goal
Increase confidence the output is actually good: validate preview behavior, basic responsive/a11y checks, and avoid regressions.

## When to use
- Use when changes affect UI/UX, routing, state handling, or anything demo-facing.
- Do not use for pure content edits unless requested.

## Steps
1) **Define what to validate**
   - Key routes, key states, and any breakpoints.
2) **Run available checks**
   - Use built-in preview quality checks.
   - If browser audit runtime is available, prefer it. Otherwise be explicit about HTML-only fallback.
3) **Fix the top issues**
   - Layout breaks, missing states, broken links.
4) **Leave traceable output**
   - Summarize what was checked and what remains uncertain.

## Tool usage
- Local tools for verifying routes/components quickly.
- MCP browser audit optional if configured.

## Guardrails
- Capability honesty: do not claim browser automation if it is not available.
- Keep validation bounded and focused on changed flows.
