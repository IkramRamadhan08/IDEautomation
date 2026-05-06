# <Skill Title>

## Persona
- Role: <who you are while using this skill>
- Tone: <short style notes>

## Goal
<What “done” looks like. Include quality bar and what must be true when finished.>

## When to use
- Use this skill when:
  - <trigger phrases, intents, or contexts>
- Do not use when:
  - <when it would cause scope creep or wrong intent>

## Steps
1) **Confirm intent + success criteria**
   - Restate the target outcome, constraints, and what we will not do.
2) **Gather evidence (read-only first)**
   - Identify the minimum files and facts needed.
   - Prefer tools before assumptions.
3) **Decide a plan (bounded)**
   - Choose the smallest coherent set of changes/actions.
   - If tools are needed first, return tool actions first and keep `changes` empty.
4) **Execute**
   - Implement with consistent patterns and names.
5) **Verify**
   - Ensure build/preview sanity.
   - Check UX states (loading/empty/error), a11y basics, and broken imports.

## Tool usage
### Local tools (preferred for repo inspection)
- `repo_search` → find entrypoints and call chains.
- `repo_read` → verify exact file contents.
- `repo_list` → quick structure view (bounded).

### MCP (optional)
- MCP is a protocol layer to connect to external tools/data sources.
- Use MCP only when a server is configured and it materially improves the result.

### Shell (suggestions only)
- Use `shell` actions only when truly needed (install/build/lint). Keep them minimal.

## Guardrails
- Default to evidence-based work, no guessing.
- Keep scope aligned to the user’s request and the current intent (inspection vs command).
- Capability honesty: do not claim runtimes/tools exist unless verified.
- Keep tool loops bounded and avoid broad scans.
