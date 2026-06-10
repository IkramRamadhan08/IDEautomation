# App Shell Professional Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the remaining oversized app shell and stylesheet into focused, maintainable units without changing runtime behavior.

**Architecture:** Keep `App.tsx` as the composition owner while moving reusable shell behavior into small hooks under `src/app/hooks`. Keep `src/app/app.css` as the ordered style manifest and move large contiguous style regions into responsibility-based files under `src/app/styles`.

**Tech Stack:** React 19, TypeScript, Vite, CSS, ESLint.

---

### Task 1: App Shell Hooks

**Files:**
- Create: `src/app/hooks/useAppTheme.ts`
- Create: `src/app/hooks/useRoutePath.ts`
- Create: `src/app/hooks/useAssistPaneResize.ts`
- Create: `src/app/hooks/useAgentLiveFeed.ts`
- Modify: `src/app/App.tsx`

- [x] Move theme persistence and toggle logic into `useAppTheme`.
- [x] Move route path and browser popstate handling into `useRoutePath`.
- [x] Move assist pane width clamping and drag-resize side effects into `useAssistPaneResize`.
- [x] Move agent live id generation, push, append, and reset helpers into `useAgentLiveFeed`.
- [x] Run `npm run build` and `npm run lint`.

### Task 2: CSS Surface Split

**Files:**
- Create: `src/app/styles/workspace.css`
- Create: `src/app/styles/landing.css`
- Create: `src/app/styles/projects.css`
- Create: `src/app/styles/settings.css`
- Create: `src/app/styles/agent.css`
- Modify: `src/app/app.css`

- [x] Move workspace/terminal/full-agent/agent-live contiguous style blocks to `workspace.css`.
- [x] Move landing page blocks to `landing.css`.
- [x] Move workspace gate, saved projects, and new project modal blocks to `projects.css`.
- [x] Move settings modal blocks to `settings.css`.
- [x] Move floating agent orb blocks and keyframes to `agent.css`.
- [x] Keep responsive overrides and cross-cutting light-theme overrides in `app.css`.
- [x] Run CSS brace scan, `npm run build`, and `npm run lint`.

### Task 3: Final Maintenance Scan

**Files:**
- Modify only if verification finds a concrete issue.

- [x] Remove empty directories and generated caches.
- [x] Scan for stale import paths.
- [x] Run `git diff --check`.
- [x] Record final file size boundaries.
