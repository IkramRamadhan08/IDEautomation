# Development Roadmap

## Tujuan Lanjutan

Tujuan lanjutan Appora adalah membuat codebase makin mudah dirawat tanpa kehilangan kemampuan agentic yang sudah ada. Fokusnya:

- pecah file besar,
- jaga regression suite tetap hijau,
- stabilkan hosted mode,
- tingkatkan reliability agent repair loop,
- perkuat observability dan docs.

## Status Hari Ini

Sudah selesai:

- Frontend dipisah ke `app`, `features`, dan `shared`.
- CSS dipisah per area UI.
- Backend root file dipindah ke domain folder:
  - `auth`
  - `config`
  - `preferences`
  - `projects`
  - `storage`
  - `workspace`
  - `assets`
  - `command`
  - `diagnostics`
- Railway/Docker leftover yang tidak dipakai sudah dihapus.
- Temp benchmark artifacts sudah dibersihkan.
- Config aktif Vercel/Supabase/TypeScript tetap dipertahankan.
- Regression suite backend tetap hijau.

## Prioritas 1: Pecah `api/main.py`

`api/main.py` masih terlalu besar. Pecah secara incremental:

### 1. Preview Runner

Target folder:

```text
api/preview/
  __init__.py
  runner.py
  audit.py
  schemas.py
```

Pindahkan:

- `RunStartReq`
- run detect/start/proxy/list/logs/stop/close route
- runner capacity helpers
- package manager launch helpers yang hanya dipakai preview runner

Acceptance:

- route behavior sama,
- preview runner regression tetap hijau,
- no circular import.

### 2. Filesystem API

Target folder:

```text
api/files/
  router.py
  schemas.py
  apply.py
  checkpoints.py
```

Pindahkan:

- fs list/read/write/diff
- apply_many/preflight
- agent harness apply
- checkpoint list/restore
- project export bila cocok

Catatan: test mengimport `ApplyManyReq`, `WriteOp`, `fs_apply_many`, `_preflight_apply_many` dari `api.main`. Pertahankan re-export atau update test dengan sengaja.

### 3. Supabase RAG Router

Target:

```text
api/rag/router.py
```

Pindahkan:

- `/api/supabase/rag/status`
- `/api/supabase/rag/sync`

Catatan: test mengimport `supabase_rag_status` dari `api.main`, jadi butuh alias kompatibilitas.

### 4. Agent Jobs Router

Target:

```text
api/agent_jobs/router.py
api/agent_jobs/worker.py
```

Pindahkan:

- job status,
- job events,
- observability,
- worker auth,
- worker run endpoint.

### 5. Agent Execution Harness

Target:

```text
api/agent_execution/
  apply.py
  shell.py
  repair.py
  completion.py
  gates.py
```

Pindahkan deterministic gates, quick repairs, failure analysis, repair rollback, and completion report. Ini paling riskan, lakukan terakhir.

## Prioritas 2: Pecah `src/app/App.tsx`

Target:

```text
src/app/hooks/
  useWorkspaceState.ts
  useProjectActions.ts
  useSettingsState.ts
  useHostedProjects.ts
  usePreviewRunner.ts
  useFileEditorState.ts
  useAgentSession.ts
```

Aturan:

- Hook harus punya boundary jelas.
- Jangan bikin global store baru dulu kecuali prop drilling sudah benar-benar menghambat.
- Setelah tiap extraction, jalankan lint/build.

Acceptance:

- `App.tsx` turun signifikan dari 1400 baris.
- Tidak ada perubahan UX.
- Agent workflow tetap tidak double-apply.

## Prioritas 3: Stabilkan Hosted Mode

Kerjakan:

- explicit capability matrix local vs hosted,
- clear warning untuk Supabase partial config,
- stronger job resume tests,
- binary asset persistence decision,
- workspace hydration/sync tests,
- worker auth docs.

Acceptance:

- User tahu fitur mana yang live di hosted.
- Provider secrets tidak silently unsafe.
- Job events cukup untuk debug run gagal.

## Prioritas 4: Preview Quality

Kerjakan:

- Pisah preview audit dari `main.py`.
- Tambah tests untuk browser vs HTML fallback.
- Buat evidence level:
  - browser visual evidence,
  - Playwright evidence,
  - HTML fallback evidence,
  - source-only evidence.
- Jangan klaim visual pass jika hanya source fallback.

Acceptance:

- Completion report jujur soal level evidence.
- Preview repair target lebih actionable.

## Prioritas 5: Agent Runtime Reliability

Kerjakan:

- Kurangi heuristik tersebar.
- Pindah failure analysis ke module sendiri.
- Tambah fixture untuk repeated failure signatures.
- Tambah tests untuk no-work recovery, strict retry, and autonomous continuation budget.

Acceptance:

- Repair loop makin deterministic.
- Stop reason selalu jelas.
- Tidak ada infinite/ambiguous progress.

## Prioritas 6: Documentation

Tambahkan docs berikut:

- API route map generated/manual.
- Agent event contract.
- Hosted/local capability matrix.
- Supabase setup guide step-by-step.
- Troubleshooting guide untuk provider/router/Supabase/preview.

## Urutan Aman Untuk AI Agent Berikutnya

1. Baca `docs/architecture/README.md`.
2. Jalankan `git status --short`.
3. Jangan revert perubahan existing tanpa instruksi user.
4. Pilih satu domain kecil.
5. Extract route/schema dulu.
6. Pertahankan compatibility alias di `api/main.py` jika test lama import langsung.
7. Jalankan regression.
8. Bersihkan cache/build artifacts.
9. Update docs bila boundary berubah.

## Definition Of Done

Sebuah refactor dianggap selesai bila:

- Struktur folder lebih sesuai domain.
- Tidak ada file yatim/orphan baru.
- Tidak ada route hilang.
- Regression suite pass.
- Lint/build pass bila frontend tersentuh.
- Docs diperbarui.
- Final report menyebut file/domain yang berubah dan verification yang dijalankan.
