# Appora Architecture

Folder ini menjelaskan arsitektur Appora setelah refactor besar 2026-06-10. Tujuannya bukan sekadar dokumentasi statis, tapi peta kerja untuk developer atau AI agent berikutnya.

## Dokumen

- [system-overview.md](system-overview.md): gambaran produk, tujuan, boundary utama, dan flow end-to-end.
- [frontend-architecture.md](frontend-architecture.md): struktur React/Vite, pembagian feature, state utama, dan titik refactor lanjutan.
- [backend-architecture.md](backend-architecture.md): struktur FastAPI, router/domain, area `api/main.py`, dan batas module backend.
- [agent-runtime.md](agent-runtime.md): lifecycle Appora Agent, fase runtime, verifier, tool usage, memory, dan auto-execute.
- [data-and-storage.md](data-and-storage.md): Supabase, local workspace, secrets, project persistence, RAG, dan job/event storage.
- [deployment-and-ops.md](deployment-and-ops.md): Vercel, local dev, scripts, testing, benchmark, dan operational constraints.
- [development-roadmap.md](development-roadmap.md): lanjutan pengembangan prioritas, urutan refactor, dan acceptance criteria.

## Prinsip Arsitektur

1. Feature code harus punya folder/domain sendiri.
2. `api/main.py` hanya boleh menjadi composition layer dan temporary host untuk legacy-heavy flows.
3. Frontend shell boleh mengorkestrasi, tapi detail domain harus turun ke hooks/components/features.
4. Agent harus traceable: setiap keputusan penting punya evidence, ledger, warning, atau event.
5. Hosted mode harus aman: workspace, secrets, RLS, dan command policy tidak boleh longgar.
6. Perubahan besar harus disertai regression test atau minimal verifikasi build/lint/test.

## Status Singkat

Appora saat ini sudah cukup rapi untuk dilanjutkan:

- Frontend sudah dipisah ke `src/app`, `src/features`, dan `src/shared`.
- CSS sudah dipisah berdasarkan area UI.
- Backend sudah mulai modular: `auth`, `config`, `projects`, `preferences`, `storage`, `workspace`, `assets`, `command`, `diagnostics`.
- `api/main.py` masih besar dan menjadi target refactor backend berikutnya.
- Test regression backend berjumlah 318 dan menjadi pagar utama untuk refactor.
