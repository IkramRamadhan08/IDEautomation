# Backend Architecture

## Stack

Backend menggunakan:

- FastAPI
- Pydantic
- Python standard library untuk filesystem/process/http utilities
- Supabase Python client
- Local subprocess execution untuk validation/preview runner
- Unittest regression suite

Entrypoint hosted berada di `api/index.py`, yang mengarah ke FastAPI app dari `api/main.py`.

## Struktur Backend Saat Ini

```text
api/
  main.py
  index.py
  settings.py
  fs.py
  app_state.py
  oauth_runtime.py
  hybrid.py

  assets/
  auth/
  command/
  config/
  diagnostics/
  preferences/
  projects/
  storage/
  workspace/

  agent.py
  agent_runtime.py
  agent_intent.py
  agent_memory.py
  agent_mcp.py
  agent_observability.py
  agent_planner.py
  agent_router.py
  agent_skills.py
  agent_tools.py
  agent_evals.py
  agent_benchmarks.py
  aider_benchmarks.py
```

## Router/Domain Yang Sudah Dipisah

### `api/diagnostics`

Memegang:

- `/api/healthz`
- `/api/auth/debug`

Ini sengaja kecil agar health/debug tidak ikut terseret domain agent.

### `api/auth`

Memegang:

- identity parsing,
- request user resolution,
- hosted auth policy,
- Google auth router.

`api/auth/policy.py` penting untuk memastikan route sensitif di hosted mode membutuhkan user valid.

### `api/config`

Memegang settings/provider route:

- model/provider config,
- API key status,
- Supabase readiness warning,
- provider secret updates,
- 9router-related settings.

### `api/preferences`

Memegang user/project preferences. Domain ini berhubungan dengan provider choice, access mode, dan preferensi project.

### `api/projects`

Memegang project list/create/rename/duplicate/archive/snapshot dan project templates.

### `api/storage`

Memegang:

- Supabase client/admin helpers,
- project file persistence,
- profiles,
- project members,
- agent jobs/events,
- agent memory chunks,
- provider secret encryption/decryption.

### `api/workspace`

Memegang:

- identity response,
- workspace get/set/clear/provision,
- native directory picker,
- browser-folder import.

Helper provisioning masih di `main.py` karena juga dipakai project creation dan `_ws`.

### `api/command`

Memegang:

- command policy route,
- terminal run route,
- agent harness shell route,
- shared shell action/policy schema.

`main.py` tetap re-export beberapa schema/wrapper untuk test dan legacy internal contract.

### `api/assets`

Memegang:

- image upload route,
- filename/alias sanitizer,
- image asset response schema.

## Yang Masih Berada Di `api/main.py`

`api/main.py` masih memegang beberapa domain besar:

- session middleware,
- hosted project hydration/persistence,
- package manager detection,
- preview dependency preparation,
- local runner start/proxy/log/stop,
- filesystem list/read/write/diff/apply_many,
- checkpoint list/restore,
- preview audit,
- Supabase RAG status/sync route,
- project validation route,
- agent capabilities/scaffold/prd,
- agent jobs/events/observability route,
- worker route,
- backend auto-execute and repair loop,
- main `/api/agent` streaming endpoint.

Ini masih besar karena banyak helper saling memanggil. Pemecahan berikutnya harus incremental dan selalu pakai regression test.

## Route Groups Dalam `main.py`

Route yang masih langsung didekorasi di `main.py`:

- `/api/run/*`
- `/api/fs/*`
- `/api/projects/export`
- `/api/checkpoints`
- `/api/preview/audit`
- `/api/supabase/rag/*`
- `/api/project/validate`
- `/api/agent/capabilities`
- `/api/agent/scaffold`
- `/api/agent/prd`
- `/api/agent/jobs/*`
- `/api/agent/worker/run`
- `/api/agent`

## Backend Request Flow

Typical request flow:

1. Request masuk ke FastAPI.
2. `VoiceIDESessionMiddleware` resolve session/user/profile context.
3. Hosted-sensitive route dicek dengan auth policy.
4. Router memanggil domain helper.
5. Jika route butuh workspace, `_ws()` memastikan workspace aktif.
6. Jika hosted project file persistence aktif, project dihydrate dari Supabase.
7. Operation dijalankan.
8. Hosted file sync/checkpoint/job event disimpan bila relevan.

## Filesystem Boundary

`api/fs.py` menyediakan:

- `safe_join`
- `list_tree`
- `read_text`
- `write_text`
- `diff_text`

Semua operasi path harus lewat `safe_join` atau validasi equivalent. Jangan menerima absolute path user di hosted mode.

## Command Policy

Command execution tidak boleh bebas. Policy saat ini:

- Safe commands: npm/pnpm/yarn/bun install/run/test, Python compile/pytest/unittest, Go/Cargo/Maven/Gradle/etc validation commands, git read-only commands, safe read commands.
- Blocked commands: `rm`, `sudo`, `su`, `dd`, `mkfs`, `mount`, `shutdown`, `kill`, dan sejenisnya.
- Trusted project mode bisa membuka command project-scoped lebih luas, tapi tetap bukan izin destruktif global.

Command route sudah dipindah ke `api/command/router.py`; policy decision helper masih di `main.py` karena dipakai banyak execution/repair helper.

## Preview Runner

Preview runner mendeteksi stack project dan menjalankan dev server pada port private. Flow:

1. Detect package manager.
2. Detect launch kind: dev script, preview script, Vite fallback, static fallback.
3. Pastikan dependency siap.
4. Start process.
5. Simpan runner record di session.
6. Proxy preview via `/api/run/proxy/{run_id}`.

Preview runner masih di `main.py`; target refactor: `api/preview/runner.py`.

## Preview Audit

Preview audit menggabungkan:

- browser audit via `agent-browser` bila tersedia,
- Playwright fallback bila tersedia,
- HTML/source fallback bila browser audit tidak siap,
- quality checks untuk responsive, a11y, starter residue, generic copy, dynamic state, image, tap target, overflow, dan product depth.

Preview audit masih di `main.py`; target refactor: `api/preview/audit.py`.

## Backend Refactor Rules

Jika memecah `main.py`:

1. Jangan pindahkan semua sekaligus.
2. Pindahkan route wrapper dulu, helper belakangan.
3. Gunakan dependency injection untuk helper yang masih di `main.py`.
4. Pertahankan alias public yang dipakai test.
5. Jalankan minimal:
   - `python3 -m compileall -q api`
   - `npm run test:agent-regression`
6. Untuk perubahan frontend ikut jalankan:
   - `npm run lint`
   - `npm run build`

## Risiko Backend

- `main.py` masih terlalu gemuk.
- Worker, jobs, auto-execute, preview audit, and repair loop masih saling terhubung kuat.
- Hosted/local mode punya branch berbeda.
- Supabase optional: code harus aman ketika Supabase tidak configured.
- Command execution harus tetap guarded.
