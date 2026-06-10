# System Overview

## Apa Itu Appora

Appora adalah hosted AI coding workspace untuk membuat, memperbaiki, memvalidasi, dan menjalankan project web/app secara agentic. Produk ini menggabungkan frontend workspace, backend orchestration, AI agent runtime, local tool execution, preview runner, Supabase persistence, dan deployment path ke Vercel.

Target utamanya: user bisa memberi instruksi natural language, lalu Appora Agent membaca project, membuat rencana, mengedit file, menjalankan validasi, memperbaiki error, dan memberi laporan berbasis bukti.

## Tujuan Produk

Appora bukan landing page generator. Arah produknya adalah AI app builder yang:

- Menghasilkan aplikasi nyata, bukan mockup marketing.
- Bisa membaca dan memodifikasi workspace project.
- Bisa menjalankan command validasi secara terkendali.
- Bisa melihat preview dan melakukan audit kualitas UI.
- Bisa menyimpan project, preferensi, job, event, dan memory.
- Bisa berjalan lokal dan hosted/serverless.

## Boundary Besar

Appora terdiri dari enam lapisan utama:

1. Frontend application shell
   - React/Vite UI.
   - Mengatur workspace, project, settings, agent pane, preview, file explorer, dan editor.

2. Backend API
   - FastAPI service di `api/index.py`/`api/main.py`.
   - Menyediakan route workspace, filesystem, runner, agent, settings, projects, assets, jobs, dan diagnostics.

3. Agent runtime
   - `api/agent_runtime.py` menjalankan pipeline agent.
   - Mengelola intent, memory, planning, local tools, MCP hints, draft, refinement, verifier, strict retry, dan finalization.

4. Execution harness
   - Backend menerapkan perubahan file, menjalankan shell command yang lolos command policy, membuat checkpoint, dan menjalankan repair loop.

5. Storage layer
   - Local filesystem untuk workspace.
   - Supabase untuk hosted persistence, auth/profile, settings, secrets, project files, jobs, events, dan RAG memory.

6. Deployment/ops
   - Vercel untuk hosted frontend dan Python API function.
   - Cron Vercel untuk worker job.
   - Scripts lokal untuk dev server, API server, router, benchmark, dan audit.

## Flow End-to-End

Flow umum ketika user meminta agent membangun/memperbaiki project:

1. User mengirim prompt dari frontend.
2. Frontend memanggil API agent dan membuka stream event.
3. Backend memastikan workspace aktif.
4. Runtime agent menyiapkan context:
   - intent classification,
   - project instructions,
   - file context,
   - memory,
   - skill hints,
   - MCP hints,
   - local tool context.
5. Agent membuat draft output terstruktur.
6. Backend verifier memeriksa output:
   - perubahan file masuk akal,
   - tidak rewrite berlebihan,
   - tidak ada fake business data,
   - validation plan tersedia,
   - preview quality criteria dipertimbangkan.
7. Jika `auto_execute` aktif, backend:
   - apply perubahan,
   - buat checkpoint,
   - jalankan shell validation,
   - audit preview jika relevan,
   - repair otomatis jika gagal.
8. Frontend menerima event progress, tool output, result, dan final message.
9. Supabase/local memory menyimpan ringkasan run untuk context berikutnya.

## Peta Komponen Saat Ini

```text
src/
  app/                  React app shell, global app state, route/layout hooks
  features/
    agent/              agent UI/runtime bridge/workflow
    preview/            preview pane and frontend preview runtime
    settings/           settings modal
    workspace/          workspace UI: explorer, editor, topbar, modes
  shared/
    api/                API client and Supabase frontend client
    types/              shared TypeScript types

api/
  main.py               main composition + legacy-heavy backend flows
  agent_runtime.py      bounded agent pipeline and driver
  agent_*.py            intent, tools, memory, skills, planner, evals, benchmarks
  auth/                 auth identity/policy/router
  config/               settings/provider config router
  projects/             project CRUD/templates/store
  preferences/          user/project preferences
  storage/              Supabase and secret storage
  workspace/            workspace/identity routes
  command/              shell command policy and harness route
  assets/               uploaded image assets
  diagnostics/          health/auth debug
```

## Arsitektur Setelah Refactor

Refactor terakhir membuat codebase lebih profesional dengan memindahkan file berdasarkan fungsi:

- Frontend tidak lagi flat di `src/components`, `src/agent`, dan `src/modes`.
- Backend tidak lagi punya semua route kecil di root `api`.
- Config Supabase/Vercel/TypeScript dipertahankan karena masih aktif.
- Railway/Docker leftovers dan temp benchmark output sudah dihapus.

Yang masih perlu dikurangi:

- `api/main.py` masih sekitar 9000 baris.
- `src/app/App.tsx` masih sekitar 1400 baris.
- Agent backend execution/repair loop masih tightly coupled dengan route, verifier, filesystem, preview audit, dan job worker.

## Non-Goals Saat Ini

Hal yang belum menjadi target langsung:

- Multi-repo monorepo architecture.
- Full plugin marketplace internal.
- Realtime collaborative editing multi-user.
- Sandboxed remote execution yang benar-benar terisolasi per user.
- Production-grade billing/payment.

Non-goals ini bisa berubah, tapi jangan refactor terlalu jauh sebelum core Appora Agent stabil.
