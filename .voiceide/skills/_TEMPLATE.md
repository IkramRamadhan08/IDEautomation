# <Skill Title>

## Persona
- Role: <who you are while using this skill>
- Tone: <short style notes>

## Goal
<What “done” looks like, in one paragraph.>

## When to use
- Use this skill when:
  - <trigger 1>
  - <trigger 2>
- Do not use when:
  - <non-trigger 1>

## Steps
1) **Clarify the target**
   - Restate the concrete outcome and constraints.
2) **Gather evidence (read-only first)**
   - Identify the minimum files/info needed.
3) **Decide the smallest viable change / action plan**
   - Prefer scoped, reversible moves.
4) **Execute**
   - If tools are needed, request tool actions first and keep `changes` empty until results are in.
5) **Verify**
   - Check for broken imports, missing references, and user-visible UX states.

## Tool usage
### Local tools (preferred for repo inspection)
- `repo_list` → quickly see what exists.
- `repo_search` → find relevant code paths before editing.
- `repo_read` → confirm exact file contents.

### MCP (optional)
- Use MCP only when an MCP server is configured and it materially improves the result.
- MCP is a protocol layer, not a tool itself. MCP servers expose tools.

## Guardrails
- Default to **read-only** until you have enough evidence.
- Do not invent files/paths/APIs without verifying via repo tools.
- If the user asked for an audit/review/explain/check, keep `changes` and `actions` empty.
- Keep actions bounded (avoid tool loops, avoid broad scans).
