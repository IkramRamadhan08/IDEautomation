# Data And Storage

## Storage Model

Appora punya dua mode storage:

1. Local/dev mode
   - Workspace berada di filesystem lokal.
   - Settings bisa dibaca dari `.env`.
   - Memory bisa disimpan lokal di `.voiceide`.

2. Hosted/serverless mode
   - Workspace dikelola di temporary/serverless storage.
   - Project files disinkronkan ke Supabase.
   - User/profile/preferences/secrets/job/event/memory disimpan di Supabase.

Kode harus selalu aman ketika Supabase belum configured.

## Supabase Files

SQL docs berada di:

```text
docs/supabase/schema.sql
docs/supabase/agent-rag.sql
docs/supabase/agent-jobs.sql
docs/supabase/provider-models.sql
```

`schema.sql` adalah schema utama. File lain menambah capability tertentu:

- `agent-rag.sql`: memory chunks/RAG.
- `agent-jobs.sql`: job/event worker support.
- `provider-models.sql`: provider/model catalog support.

## Supabase Tables Utama

Table penting:

- `profiles`
- `projects`
- `project_members`
- `user_settings`
- `project_preferences`
- `project_files`
- `user_provider_secrets`
- `agent_jobs`
- `agent_job_events`
- `agent_memory_chunks`

## Profile And Identity

Auth identity di-resolve oleh backend melalui `api/auth/identity.py` dan middleware di `api/main.py`.

Ada beberapa konsep identity:

- request user,
- current user id,
- current profile id,
- session id,
- Supabase user id.

Hosted profile id penting karena provider secrets/preferences harus terkait internal profile, bukan sekadar UUID legacy yang bisa berubah konteks.

## Project Persistence

Local mode:

- Project berada langsung di workspace.
- File read/write lewat filesystem.

Hosted mode:

- Text file project bisa dihydrate dari Supabase.
- Setelah shell/apply, backend melakukan best-effort sync text files.
- Binary/large files tidak otomatis dipersist seperti text files.

Helpers terkait berada di `api/main.py` dan `api/storage/supabase.py`.

## Project Files

`project_files` menyimpan path dan content text file per project. Path harus project-relative dan aman. Hosted hydration akan membuat file lokal sementara agar runner/tools bisa bekerja seperti project normal.

## Preferences

Preferences terbagi:

- user-level preferences,
- project-level preferences.

Contoh project preference yang penting: `agent_access_mode`, misalnya safe/trusted.

## Provider Secrets

Secrets dikelola di:

- `api/storage/secrets.py`
- table `user_provider_secrets`

Hosted mode harus memakai secret encryption key (`VOICEIDE_SECRET_KEY`) agar BYOK provider key tidak disimpan plaintext. Jika secret storage belum siap, settings route harus memberi warning.

## Agent Jobs

Background job flow:

1. Request agent bisa dibuat sebagai job.
2. Job record masuk `agent_jobs`.
3. Event masuk `agent_job_events`.
4. Worker route `/api/agent/worker/run` mengambil pending/running jobs.
5. Cron Vercel memanggil worker harian saat configured.

Job/event penting untuk hosted long-running work karena serverless request punya batas durasi.

## Agent Memory

Memory bisa berasal dari:

- local project memory,
- local short-term memory,
- Supabase `agent_memory_chunks`.

RAG readiness perlu dicek:

- env Supabase siap,
- service role key siap,
- table ada,
- optional vector extension tersedia.

Jika RAG table tidak ada, Appora tetap harus jalan dengan warning.

## RLS And Security

Supabase schema memakai RLS policies. Tujuannya:

- User hanya bisa membaca/mengubah data sendiri.
- Project member access terkontrol.
- Service role backend tetap bisa melakukan operasi server-side.

Jangan membuat backend route yang menerima arbitrary owner id dari client tanpa resolve request user.

## Workspace Boundary

Path safety sangat penting:

- Semua path user harus normalized.
- Tidak boleh keluar workspace.
- Tidak boleh menerima `..`, absolute path, atau symlink escape tanpa validasi.
- Hosted arbitrary folder picking disabled.

`safe_join` dari `api/fs.py` adalah guard utama.

## Data Risks

- Supabase optional state membuat banyak branch.
- Hosted hydration/sync bersifat best-effort.
- Binary assets belum memiliki persistence story yang sama kuat dengan text files.
- Secret key missing harus terlihat jelas sebagai warning, bukan silent insecure fallback.
- Worker/job resume harus menjaga profile context.

## Pengembangan Lanjutan Storage

Prioritas:

1. Buat storage capability matrix: local vs hosted.
2. Dokumentasikan file size/text/binary policy.
3. Tambah tests untuk hydration/sync edge cases.
4. Pisahkan hosted project file helpers dari `main.py` ke `api/storage/project_files.py`.
5. Tambah migration/version marker untuk schema SQL.
