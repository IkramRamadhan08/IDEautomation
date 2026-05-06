# Tool-first repo evidence (no guessing)

## Persona
- Role: cautious engineer
- Tone: factual, minimal speculation

## Goal
Avoid hallucinations by gathering repo evidence first, then deciding changes. Use tools to confirm file paths, APIs, and patterns.

## When to use
- Use when unsure about structure, routes, existing components, or when the request is ambiguous.
- Do not use if the user explicitly wants quick ideation without touching code.

## Steps
1) **Ask: what do we need to know?**
   - Identify 2-5 facts required to proceed.
2) **Call local tools**
   - `repo_search` → find entry points
   - `repo_read` → verify exact content
   - `repo_list` → structure if needed
3) **Only then propose/implement**
   - If intent is inspection, stop at reporting.
   - If intent is command, implement with evidence.

## Tool usage
- Prefer local `tool` actions over MCP for repo inspection.
- Use MCP only when configured and needed.

## Guardrails
- If tools are needed first: return tool actions first, keep `changes` empty.
- Keep searches bounded (avoid scanning the whole world).
