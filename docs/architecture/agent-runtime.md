# Agent Runtime

## Lokasi Utama

Agent runtime utama berada di:

- `api/agent_runtime.py`
- `api/agent_intent.py`
- `api/agent_memory.py`
- `api/agent_tools.py`
- `api/agent_skills.py`
- `api/agent_mcp.py`
- `api/agent_planner.py`
- `api/agent_observability.py`
- `api/main.py` untuk backend execution harness, auto-execute, repair loop, dan route `/api/agent`.
- `src/features/agent/workflow.ts` untuk frontend orchestration dan stream handling.

## Konsep Utama

Appora Agent dibuat sebagai bounded agent, bukan infinite autonomous loop. Runtime harus:

- punya fase eksplisit,
- punya batas LLM call dan driver step,
- mengeluarkan trace/event,
- memakai tool dengan alasan,
- memverifikasi output,
- menghindari fake progress,
- bisa berhenti dengan status blocked bila evidence tidak cukup.

## Pipeline Tingkat Tinggi

Pipeline runtime:

1. Prepare context
2. Intent classification
3. Memory retrieval
4. Skill matching
5. MCP hinting
6. Planning
7. Deep preflight tools
8. Read-only scouts
9. Draft
10. Tooling/refine
11. Verify
12. Strict retry/autonomous continuation bila perlu
13. Finalize

## Agent Driver

`AgentDriver` mengelola fase runtime dengan finite loop. Fase utama:

- `draft`
- `tooling`
- `refine`
- `verify`
- `strict_retry`
- `autonomous_continue`
- `finalize`

`AgentRunController` membatasi jumlah step, LLM call, dan autonomous continuation supaya run tidak liar.

## Context Preparation

Context preparation mengumpulkan:

- prompt user,
- selected project root,
- workspace file context,
- active file/editor context,
- selected asset paths,
- project instructions dari `.voiceide/skills`,
- active work state,
- local memory,
- Supabase memory bila siap,
- settings/provider state,
- validation plan,
- detected stack.

Runtime harus tahan terhadap file yang tidak bisa dibaca. Warning harus disimpan ke trace, bukan langsung crash kecuali request tidak valid.

## Memory

Memory terbagi:

- Short-term/local memory: ringkasan run dan project state.
- Project profile memory: state/decision yang relevan untuk project.
- Supabase RAG memory: `agent_memory_chunks` bila table dan env siap.

Memory dipakai untuk:

- meneruskan short follow-up seperti "gas" atau "lanjut",
- menghindari repeat broken approach,
- mengingat stack project,
- mengingat active work state.

## Skills

Skill system membaca instruksi lokal dari `.voiceide/skills` dan/atau skill hints runtime. Skill bukan decorator magic; ia harus menghasilkan guidance yang dipakai agent untuk planning dan verification.

Skill yang relevan untuk Appora:

- accessibility,
- preview quality,
- routing/IA,
- Supabase integration,
- tool-first evidence,
- MCP boundaries,
- UI system,
- UX states,
- runbook/hybrid fix.

## Local Tools

Local tools dari `api/agent_tools.py` dipakai untuk:

- inspect filesystem,
- detect stack,
- read package/config,
- plan validation,
- inspect project quality,
- gather evidence sebelum edit.

Prinsip: agent harus membaca dulu sebelum mengedit. Read-only scouts membantu task kompleks supaya agent tidak hanya mengandalkan prompt.

## MCP

MCP integration ada sebagai hint/action boundary. Runtime bisa menyarankan MCP read-only actions, tapi free-tier/guard bisa menahan external MCP untuk command build. MCP tidak boleh menggantikan local validation.

## Draft Output

Agent output harus structured. Secara umum output membawa:

- spoken/final response,
- proposed file changes,
- shell actions,
- validation plan,
- assumptions,
- warnings,
- repair metadata,
- route/preview hints.

Jika LLM gagal/invalid JSON, runtime punya fallback emergency agar user tetap mendapat output executable atau blocked report.

## Verification

Verifier memeriksa:

- patch/change valid,
- tidak stale terhadap file saat ini,
- tidak rewrite berlebihan tanpa alasan,
- tidak fake business data,
- tidak terlalu banyak inline style,
- missing CSS classes,
- UI quality signals,
- validation commands,
- project-root consistency,
- preview readiness.

Verifier bukan formal proof. Ia adalah gate pragmatis sebelum backend mengizinkan apply/auto-execute.

## Auto-Execute

Jika request mengaktifkan auto-execute, backend di `main.py` melakukan:

1. Normalize changes.
2. Prepare output changes.
3. Gate fake/inert/business-data issues.
4. Apply file changes via harness.
5. Create checkpoint.
6. Run shell actions/validation.
7. Start preview bila diperlukan.
8. Run preview audit bila URL tersedia.
9. Analyze failure.
10. Run quick deterministic repair atau LLM repair.
11. Re-run validation/preview as needed.
12. Final completion report.

## Repair Loop

Repair loop harus bounded. Ia tidak boleh mencoba selamanya. Repair bisa:

- deterministic quick fix,
- shell-only retry,
- LLM repair pass,
- rollback checkpoint bila repair memperburuk hasil,
- stop dengan unresolved handoff.

Failure analysis menyimpan:

- command signatures,
- repeated failure count,
- resolved failure commands,
- suggested next action,
- preview polish debt,
- state readiness debt.

## Frontend Agent Workflow

`src/features/agent/workflow.ts` bertugas:

- membuka stream agent,
- menampilkan native progress,
- memanggil apply harness bila backend meminta,
- menjalankan preview/validation action bila diperlukan,
- menggabungkan evidence ke UI,
- mencegah double apply,
- menampilkan final response.

Frontend tidak boleh mengarang milestone. Progress harus berasal dari event backend/model/native execution.

## Observability

Agent observability terdiri dari:

- job events,
- run ledger,
- trace warnings,
- tool call/tool output events,
- command results,
- preview audit result,
- final completion criteria.

Hosted job observability ada di route `/api/agent/jobs/{job_id}/observability`.

## Risiko Runtime

- `api/agent_runtime.py` besar dan kaya heuristik.
- `api/main.py` masih memegang banyak execution helper.
- Repair loop punya banyak branch.
- Jika frontend/backend event contract berubah, UI bisa double-render atau kehilangan evidence.
- Preview audit fallback HTML/source tidak setara dengan browser visual evidence.

## Prinsip Pengembangan Agent Lanjutan

1. Jangan tambah autonomy tanpa budget/stop reason.
2. Jangan tambah tool tanpa event/evidence.
3. Jangan tambah repair branch tanpa regression test.
4. Jangan biarkan frontend mengarang hasil.
5. Jangan bypass command policy untuk convenience.
6. Setiap blocked state harus jelas butuh input apa.
