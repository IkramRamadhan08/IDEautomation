# Frontend Architecture

## Stack

Frontend menggunakan:

- React 19
- TypeScript 5
- Vite 7
- Framer Motion
- Lucide React
- Supabase JS client
- Sonner
- React Draggable

Build config berada di `vite.config.ts`. Build sengaja memakai `minify: false` karena minification pernah boros memory di mesin dev/benchmark. Rollup manual chunks dipakai untuk React, motion, Supabase, dan icons.

## Struktur Folder

```text
src/
  main.tsx
  app/
    App.tsx
    app.css
    appDefaults.ts
    feedback.ts
    components/
    hooks/
    styles/
  features/
    agent/
      components/
      liveActions.ts
      runtime.ts
      workflow.ts
    preview/
      components/
      runtime.ts
    settings/
      components/
    workspace/
      components/
      modes/
  shared/
    api/
      client.ts
      supabase.ts
    types/
      index.ts
```

## App Shell

`src/app/App.tsx` adalah pusat state dan orkestrasi UI. File ini masih besar, tapi sudah lebih bersih karena beberapa bagian dipisah:

- `components/ApporaLoading.tsx`
- `components/LandingGate.tsx`
- `components/ProjectModals.tsx`
- `components/WorkspaceOnboarding.tsx`
- `hooks/useAgentLiveFeed.ts`
- `hooks/useAppTheme.ts`
- `hooks/useAssistPaneResize.ts`
- `hooks/useRoutePath.ts`
- `appDefaults.ts`

Tanggung jawab `App.tsx` saat ini:

- Menentukan auth/local dev identity.
- Menyimpan state workspace, project, hosted projects, settings, dan route.
- Mengatur mode UI: landing, hybrid workspace, full agent workspace.
- Menghubungkan agent workflow ke UI.
- Mengatur file explorer/editor state.
- Mengatur preview run state.
- Mengatur modal/settings/project actions.

## Feature Agent

`src/features/agent` berisi sisi frontend dari Appora Agent:

- `workflow.ts`: orchestration utama dari prompt ke backend stream, apply harness, shell validation, preview audit, repair prompt, dan final UI updates.
- `runtime.ts`: tipe dan utilitas runtime frontend.
- `liveActions.ts`: mapping event/live action ke UI.
- `components/AgentOrb.tsx`: visual agent/orb pane.
- `components/AgentLiveStage.tsx`: stage/progress visualization.

Frontend agent tidak boleh menjadi sumber kebenaran untuk apply/validation. Sumber kebenaran tetap backend. Frontend hanya:

- Mengirim prompt dan opsi.
- Menampilkan stream event.
- Mengirim action yang memang perlu dari UI.
- Menampilkan evidence/result.

## Feature Workspace

`src/features/workspace` menampung UI kerja utama:

- `components/editor/MonacoEditor.tsx`
- `components/explorer/FileExplorer.tsx`
- `components/navigation/Topbar.tsx`
- `modes/HybridWorkspace.tsx`
- `modes/FullAgentWorkspace.tsx`

Mode workspace:

- Hybrid Workspace: user melihat agent, file tree/editor, dan preview dalam satu pengalaman.
- Full Agent Workspace: pengalaman lebih agent-first.

## Feature Preview

`src/features/preview` mengelola preview pane dan runtime frontend preview. Backend tetap bertanggung jawab atas runner process dan preview audit. Frontend preview terutama:

- Menampilkan iframe/preview URL.
- Mengirim action start/stop/reload.
- Menampilkan status running/log.

## Settings

`src/features/settings/components/SettingsModal.tsx` mengatur provider, model, Supabase readiness, key status, dan preferensi agent. Data diambil lewat `src/shared/api/client.ts`.

## Shared Layer

`src/shared/api/client.ts` adalah API client typed-ish untuk backend. `src/shared/api/supabase.ts` adalah Supabase frontend client. `src/shared/types/index.ts` menjadi pusat tipe lintas feature.

Aturan:

- Feature boleh import dari `shared`.
- `shared` tidak boleh import dari feature.
- `app` boleh compose semua feature.
- Feature sebaiknya tidak saling import langsung kecuali ada alasan jelas.

## CSS Architecture

CSS sudah dipisah:

```text
src/app/styles/
  base.css
  layout.css
  loading.css
  workspace.css
  landing.css
  projects.css
  settings.css
  agent.css
```

`src/app/app.css` menjadi aggregator dan tempat style cross-cutting yang belum punya domain. Target berikutnya: kurangi sisa style global di `app.css` bila sudah jelas domainnya.

## Titik Refactor Lanjutan Frontend

Prioritas 1:

- Pecah `App.tsx` menjadi hook domain:
  - `useWorkspaceState`
  - `useProjectActions`
  - `useHostedProjects`
  - `useSettingsState`
  - `usePreviewRunner`
  - `useFileEditorState`

Prioritas 2:

- Pindahkan project modal logic dari shell ke `features/projects` bila folder itu dibuat.
- Buat `features/workspace/hooks` untuk file read/write, selected file, dirty state, dan refresh tree.

Prioritas 3:

- Kurangi prop drilling di workspace modes.
- Stabilkan event model agent UI agar tidak double-apply atau double-render final status.

## Risiko Frontend

- `App.tsx` masih terlalu banyak state dan side effect.
- `workflow.ts` agent cukup kompleks dan mudah kena regression.
- Supabase frontend auth dan local dev auth punya branch berbeda.
- UI/preview state harus sinkron dengan backend runner state agar user tidak melihat preview palsu.
