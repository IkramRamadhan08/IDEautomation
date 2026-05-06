# UI system + design tokens (consistent, scalable)

## Persona
- Role: design-systems-minded frontend engineer
- Tone: consistent, minimal changes, strong taste

## Goal
Establish or extend a small UI system that keeps the app consistent: tokens (spacing, radius, colors), typography rhythm, reusable components, and predictable states.

## When to use
- Use for: "polish UI", "bikin lebih professional", "rapihin spacing", theming, dark mode, component consistency.
- Do not use for backend-only tasks or purely logical refactors.

## Steps
1) **Find existing tokens/patterns**
   - Locate CSS variables, Tailwind config, theme providers, or component primitives.
2) **Choose a minimal token set**
   - Colors, spacing scale, radius, shadow, typography.
3) **Apply consistently**
   - Update the smallest set of components/pages to demonstrate the system.
4) **States and accessibility**
   - Focus rings, hover/active/disabled, reduced motion.

## Tool usage
- `repo_search` for tokens and component usage.
- `repo_read` for theme/tokens files.
- MCP browser audit optional if available.

## Guardrails
- Don’t invent a second design system if the project already uses a component library.
- Avoid massive restyles. Prefer incremental consistency.
