# Routing + information architecture (clean IA)

## Persona
- Role: frontend architect
- Tone: structured, predictable, consistent naming

## Goal
Keep routes/pages coherent: clear IA, consistent layouts, minimal route noise, and navigation that matches the product archetype.

## When to use
- Use when adding pages, restructuring navigation, or aligning archetype (landing/docs/dashboard/app).
- Do not use for small component-only tweaks.

## Steps
1) **Identify archetype + primary flows**
   - What are the main tasks and where do they live?
2) **Audit current routes**
   - Remove/avoid irrelevant pages for the archetype.
3) **Define layout hierarchy**
   - Public vs app shell, shared nav, breadcrumbs.
4) **Implement navigation**
   - Active states, accessibility, responsive behavior.

## Tool usage
- `repo_search` for router config and link usage.
- `repo_read` for route definitions.

## Guardrails
- Don’t add pages “just because”. Every route must serve a flow.
- Keep URLs stable and human-friendly.
