# Professional Refactor Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split large app shell/style files into focused modules without changing runtime behavior.

**Architecture:** Keep feature folders as the ownership boundary. Extract stable app-shell helpers and route/auth presentation first, then split CSS by responsibility while preserving import order through `src/app/app.css`.

**Tech Stack:** React 19, TypeScript, Vite, FastAPI, unittest, ESLint.

---

### Task 1: App Shell Helpers

**Files:**
- Create: `src/app/components/ApporaLoading.tsx`
- Create: `src/app/appDefaults.ts`
- Modify: `src/app/App.tsx`

- [x] Extract loading UI, local-dev user factory, theme type, default pane width, and settings-model resolver.
- [x] Run `npm run build` and `npm run lint`.

### Task 2: Settings Router Domain Folder

**Files:**
- Create: `api/config/__init__.py`
- Move: `api/settings_router.py` to `api/config/router.py`
- Modify: `api/main.py`
- Modify: `api/tests/test_agent_regressions.py`
- Modify: `README.md`

- [x] Move settings routes into `api/config`.
- [x] Update direct imports and test patch paths.
- [x] Run targeted hosted/settings regression tests.

### Task 3: CSS Responsibility Split

**Files:**
- Create: `src/app/styles/base.css`
- Create: `src/app/styles/layout.css`
- Create: `src/app/styles/loading.css`
- Modify: `src/app/app.css`

- [x] Move font/theme/reset rules to `base.css`.
- [x] Move shell/top-level layout rules to `layout.css`.
- [x] Move Appora loading keyframes/theme/reduced-motion rules to `loading.css`.
- [x] Keep `app.css` as ordered imports plus remaining feature styles.
- [x] Run `npm run build` and `npm run lint`.

### Task 4: Final Verification

**Files:**
- No source edits unless verification reveals a concrete issue.

- [x] Run stale path scan.
- [x] Run TS and Python orphan scans.
- [x] Run `npm run build`.
- [x] Run `npm run lint`.
- [x] Run `npm run test:agent-regression`.
- [x] Remove generated cache/temp artifacts.

### Task 5: Project Modal Extraction

**Files:**
- Create: `src/app/components/ProjectModals.tsx`
- Modify: `src/app/App.tsx`

- [x] Move new-project modal, saved-projects panel, and project-manager modal into focused presentational components.
- [x] Keep project state, mutations, and side effects owned by `App.tsx`.
- [x] Run `npm run build` and `npm run lint`.

### Task 6: Landing Gate Extraction

**Files:**
- Create: `src/app/components/LandingGate.tsx`
- Modify: `src/app/App.tsx`

- [x] Move public landing/login gate markup into a focused component.
- [x] Keep auth navigation and theme callbacks owned by `App.tsx`.
- [x] Run `npm run build` and `npm run lint`.

### Task 7: Workspace Onboarding Extraction

**Files:**
- Create: `src/app/components/WorkspaceOnboarding.tsx`
- Modify: `src/app/App.tsx`

- [x] Move signed-in onboarding gate markup into a focused component.
- [x] Keep workspace selection, project modals, and folder input behavior owned by `App.tsx`.
- [x] Run `npm run build` and `npm run lint`.

### Task 8: Final Legacy Style Prune

**Files:**
- Modify: `src/app/app.css`

- [x] Remove unused legacy auth and spline selectors after landing/onboarding extraction.
- [x] Preserve active `authLanding` and `authBrandMark` styling hooks.
- [x] Run CSS brace scan, `npm run build`, `npm run lint`, and `npm run test:agent-regression`.
