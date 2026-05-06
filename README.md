# Voice IDE

Voice IDE adalah proyek **untuk lulus kuliah**.

Arah project ini sekarang sengaja dipersempit:
- dipakai **dosen / evaluator** untuk mencoba hasil kerja
- **serverless only** lewat **Vercel**
- state utama disimpan di **Supabase**
- flow harus terasa rapi, gampang dipakai, dan cukup meyakinkan saat demo
- agent harus proper untuk bantu bikin web yang kelihatan bagus dan modern

Project ini **bukan** lagi diarahkan ke:
- server sendiri yang harus dibayar terus
- Docker/self-host setup
- infra multi-tenant production yang berat

## Fokus produk

Voice IDE punya 2 mode agent:

- **Clara** = full-agent mode, lebih cocok buat minta agent bangun web/app secara lebih menyeluruh
- **Raka** = hybrid mode, lebih cocok buat ngoding bareng dan edit bertahap

Tujuan UX sekarang:
- login lancar
- project tersimpan
- preferences tersimpan
- agent enak dipakai
- hasil web terlihat proper untuk presentasi / penilaian

## Stack

- Frontend: React + Vite + TypeScript
- Backend: FastAPI
- Auth + persistence: Supabase
- Deploy target: Vercel

## Auth dan model

Yang tetap dipakai:
- **OAuth login user/app** boleh tetap ada

Yang sudah dibuang:
- **OAuth provider model**

Akses model sekarang **BYOK only**:
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENROUTER_API_KEY`

## Ambil project

Kalau mau ambil project ini dari GitHub:

```bash
git clone <URL-REPO-KAMU>
cd voice-ide
npm install
python3 -m venv api/.venv
source api/.venv/bin/activate
pip install -e ./api
```

Jalankan local dev kalau mau cek cepat sebelum deploy:

### Frontend
```bash
npm run dev
```

### Backend
```bash
source api/.venv/bin/activate
uvicorn api.main:app --reload --host 0.0.0.0 --port 8787
```

## Deploy ke Vercel serverless

Ini jalur deploy utama project ini.

### 1. Siapkan Supabase

Buat project di Supabase, lalu jalankan schema dari file:

- `SUPABASE_SCHEMA.sql`
- `docs/supabase-agent-rag.sql` untuk tabel `public.agent_memory_chunks` yang dipakai agent RAG

Setelah itu siapkan Auth provider kalau kamu mau pakai login Google.

### 2. Import repo ke Vercel

- Push repo ke GitHub
- Buka Vercel
- Import repository ini
- Framework: **Vite**

Repo ini sudah punya:
- `vercel.json`
- `api/index.py`

Jadi frontend dan API sudah diarahkan untuk deploy serverless di Vercel.

### 3. Isi environment variables di Vercel

Minimal isi ini di **Vercel Environment Variables**:

```env
VITE_SUPABASE_URL=...
VITE_SUPABASE_ANON_KEY=...
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
VOICEIDE_SECRET_KEY=...  # random string, required for hosted BYOK secret encryption
```

Catatan BYOK (hosted):
- Di deploy hosted, **jangan mengandalkan** `OPENAI_API_KEY` dari env untuk semua user.
- User akan isi API key mereka di Settings, lalu key disimpan **per akun** di Supabase (`user_provider_secrets`) dengan enkripsi menggunakan `VOICEIDE_SECRET_KEY`.

Kalau mau set model/config dasar juga, tambahkan:

```env
LLM_PROVIDER=openrouter
BUILD_MODE=hybrid
OPENAI_MODEL=gpt-5.5
ANTHROPIC_MODEL=claude-opus-4-7
OPENROUTER_MODEL=x-ai/grok-4.3
GROQ_MODEL=groq/compound
GEMINI_MODEL=gemini-3-pro-preview
TOGETHER_MODEL=deepseek-ai/DeepSeek-V4-Pro
CEREBRAS_MODEL=zai-glm-4.7
XAI_MODEL=grok-4.3
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
```

Default hosted disarankan pakai OpenRouter supaya user BYOK bisa mulai dari router `openrouter/free` atau model `:free`/murah dulu, lalu upgrade model kalau butuh kualitas lebih.
OpenAI tetap ditampilkan sebagai opsi familiar; kalau akun user punya free trial/account credits, pemakaian API akan memotong credit itu dulu, tapi model OpenAI bukan free-tier unlimited.

Optional provider lain:

```env
ANTHROPIC_API_KEY=...
OPENROUTER_API_KEY=...
GROQ_API_KEY=...
GEMINI_API_KEY=...
TOGETHER_API_KEY=...
CEREBRAS_API_KEY=...
XAI_API_KEY=...
SUPABASE_ANON_KEY=...
```

### 3.5 Cek readiness Supabase agent

Backend sekarang punya jalur readiness biar nggak nebak-nebak:

- `GET /api/supabase/rag/status?project_root=.`
- `POST /api/supabase/rag/sync` dengan body `{ "project_root": "." }`

Kalau status bilang `missing`, artinya koneksi Supabase ada tapi tabel `public.agent_memory_chunks` belum dibikin.
Kalau status masih `unconfigured` tapi auth frontend sudah hidup, biasanya yang kurang adalah `SUPABASE_SERVICE_ROLE_KEY`, jadi login bisa siap tapi sync RAG/backend persistence belum live.

### 4. Deploy

Klik **Deploy** di Vercel.

### 5. Tes flow setelah live

Urutan tes yang disarankan:
- buka app
- login
- buka settings
- isi provider/model
- create project
- reload halaman
- pastikan project masih ada
- coba Hybrid mode
- coba Full Agent mode
- minta agent bikin landing page / dashboard kecil
- cek hasil preview dan kualitas flow

## Batasan yang jujur

Project ini sekarang ditargetkan untuk:
- demo
- penilaian dosen
- validasi hasil kerja kuliah

Project ini **belum** ditujukan untuk:
- SaaS production penuh
- runtime berat yang stabil untuk banyak user
- isolasi tenant kelas production
- infra berbayar jangka panjang

## Kesimpulan arah project

Arah final saat ini sederhana:

- **bukan** bangun startup infra berat
- **bukan** self-host Docker
- **bukan** bayar server sendiri terus
- **iya** untuk serverless Vercel
- **iya** untuk flow yang rapi
- **iya** untuk agent yang proper bikin web keren
- **iya** untuk dipakai presentasi dan bantu lulus kuliah
