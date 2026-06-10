# Agent Handoff - 2026-06-10

## Context

The user asked for a deep cleanup of the Appora/IDEautomation worktree: remove unused files, split code into relevant folders, reduce root clutter, and make the project easier for future agents to maintain. The refactor was done in-place in `/home/eightarch/Projects/IDEautomation`.

Current target architecture is **Vercel serverless + Supabase**, not Railway/Docker. Keep Vercel, Supabase, Vite, and TypeScript config unless a future requirement explicitly changes the deployment target.

## Main Outcome

The repository is now organized around clear ownership boundaries:

```text
src/app/                App shell, app-level components, hooks, styles
src/features/agent/     Agent UI/runtime/workflow
src/features/preview/   Preview runtime and pane
src/features/settings/  Settings UI
src/features/workspace/ Workspace/editor/explorer/topbar/modes
src/shared/             Shared frontend API clients, Supabase client, types

api/auth/               Auth identity, policy, router
api/config/             Settings/config router
api/preferences/        Preferences router and store
api/projects/           Project router/store/templates
api/storage/            Supabase and secret storage helpers
```

The old empty frontend folders were removed after moves: `src/components`, `src/agent`, `src/modes`, `src/preview`, `src/lib`, and `src/types`.

## Important Splits

Frontend app shell:

- `src/App.tsx` moved to `src/app/App.tsx`.
- `src/app/App.tsx` now composes the app shell and delegates reusable behavior to:
  - `src/app/hooks/useAppTheme.ts`
  - `src/app/hooks/useRoutePath.ts`
  - `src/app/hooks/useAssistPaneResize.ts`
  - `src/app/hooks/useAgentLiveFeed.ts`
- App shell UI pieces were extracted to:
  - `src/app/components/ApporaLoading.tsx`
  - `src/app/components/LandingGate.tsx`
  - `src/app/components/ProjectModals.tsx`
  - `src/app/components/WorkspaceOnboarding.tsx`
- App constants/defaults live in `src/app/appDefaults.ts`.

Frontend styles:

- `src/app/app.css` is now the ordered manifest plus cross-cutting styles.
- Large style regions were split into:
  - `src/app/styles/base.css`
  - `src/app/styles/layout.css`
  - `src/app/styles/loading.css`
  - `src/app/styles/workspace.css`
  - `src/app/styles/landing.css`
  - `src/app/styles/projects.css`
  - `src/app/styles/settings.css`
  - `src/app/styles/agent.css`
- Legacy unused auth/spline CSS was removed.

Backend:

- Auth files moved under `api/auth/`.
- Settings router moved under `api/config/`.
- Preferences moved under `api/preferences/`.
- Projects and templates moved under `api/projects/`.
- Supabase/secrets storage moved under `api/storage/`.
- Imports and test patch paths were updated to match the new modules.

## Removed As Unused Or Temporary

Deployment leftovers removed because current target is Vercel/Supabase:

- `.railwayignore`
- `railway.json`
- `.dockerignore`
- `Dockerfile`
- `scripts/start-railway.sh`

Other orphan/stale files removed:

- `api/oauth_bridge.mjs`
- `scripts/live_agent_loop_test.py`
- `src/components/agent/AgentAuditTrail.tsx`
- tracked `.tmp-agent-bench` benchmark artifacts

Large untracked benchmark temp directories were also cleaned. Root `.tmp-*`, `__pycache__`, `.pytest_cache`, and `*.tsbuildinfo` scans were clean at handoff time.

## Do Not Remove Without Rechecking

These are still active:

- `vercel.json`: needed for Vercel API rewrites, SPA fallback, function duration, and cron worker.
- `.vercelignore`: prevents local `.voiceide-*` state from being uploaded.
- `src/shared/api/supabase.ts`: used by `App.tsx` and `src/shared/api/client.ts` for frontend Supabase auth/session.
- `api/storage/supabase.py`: backend persistence for projects, preferences, secrets, memory/RAG, jobs.
- `tsconfig.json`, `tsconfig.app.json`, `tsconfig.node.json`: used by `npm run build` via `tsc -b`.
- `vite.config.ts`: used by Vite dev/build and manual chunking.
- `playwright.config.ts`: used by `npm test`.

Remaining `Dockerfile` text in tests or agent stack detectors is intentional fixture/detection logic for user projects, not this repo's root deployment config.

## Verification Evidence

Fresh verification was run after the final Railway/Docker cleanup:

- `npm run build` passed.
- `npm run lint` passed.
- `npm run test:agent-regression` passed with `318 tests OK`.
- `git diff --check` passed.
- Temp/cache cleanup scan was clean.

Earlier during the refactor, CSS brace scans also passed after splitting styles.

## Current Size Boundaries

Approximate current line counts after the split:

```text
src/app/App.tsx                         1439
src/app/app.css                         1745
src/app/hooks/useAgentLiveFeed.ts         57
src/app/hooks/useAppTheme.ts              19
src/app/hooks/useAssistPaneResize.ts      53
src/app/hooks/useRoutePath.ts             25
src/app/styles/agent.css                 583
src/app/styles/base.css                  118
src/app/styles/landing.css               948
src/app/styles/layout.css                 54
src/app/styles/loading.css               160
src/app/styles/projects.css              772
src/app/styles/settings.css              149
src/app/styles/workspace.css             983
```

`App.tsx` is still the largest frontend file. It is now maintainable enough, but the next professional split should extract domain hooks for project/workspace actions if further cleanup is requested.

## Recommended Next Steps

If the next agent continues cleanup, prioritize these in order:

1. Extract project actions from `src/app/App.tsx` into a focused hook such as `src/app/hooks/useProjectActions.ts`.
2. Extract file explorer/editor actions into `src/app/hooks/useWorkspaceFiles.ts`.
3. Extract settings model route logic into `src/app/hooks/useSettingsModelRoute.ts`.
4. Consider moving each feature's CSS closer to the feature only if the build/import pattern stays simple.
5. Keep Vercel/Supabase config unless the user explicitly changes the hosting target.

Avoid broad rewrites. The repo is now passing verification, and most remaining value is in small ownership-boundary extractions.

## Known Git State Note

The worktree contains many intentional deletes and renames from cleanup. Do not blindly revert deleted temp or moved files. If reviewing status, separate:

- intentional cleanup/deploy removals,
- intentional source renames/splits,
- pre-existing unrelated deletions such as older docs/templates that may have already been dirty before this cleanup.

When in doubt, inspect the diff and run the verification commands above before deciding.
