# Deployment And Ops

## Local Development

Scripts utama:

```bash
npm run dev
npm run dev:api
npm run dev:local
npm run router
```

Makna:

- `dev`: Vite frontend.
- `dev:api`: start local API tanpa frontend.
- `dev:local`: start local stack lewat `scripts/start-local.sh`.
- `router`: start 9router lewat `scripts/start-9router.sh`.

## Build

```bash
npm run build
```

Build menjalankan:

1. `tsc -b`
2. `vite build`

Vite build saat ini tidak minify untuk menjaga memory stability.

## Lint

```bash
npm run lint
```

Lint memakai ESLint flat config.

## Tests

Regression backend utama:

```bash
npm run test:agent-regression
```

Ini menjalankan:

```bash
./api/.venv/bin/python -m unittest discover -s api/tests -t . -v
```

Jumlah regression terakhir: 318 tests.

Playwright test tersedia via:

```bash
npm run test
```

Preview audit:

```bash
npm run audit:preview
```

## Benchmarks

Scripts benchmark:

```bash
npm run eval:agent
npm run bench:agent
npm run bench:agent:aider
npm run bench:agent:internal
npm run bench:agent:internal:live
```

Temporary benchmark output tidak boleh masuk repo permanen. `.tmp-agent-bench` dan `.tmp-*` adalah disposable artifacts.

## Vercel Deployment

`vercel.json`:

- framework: `vite`
- API function: `api/index.py`
- maxDuration: 300 seconds
- rewrite `/api/(.*)` ke `/api/index.py`
- SPA fallback ke `/index.html`
- cron `/api/agent/worker/run?limit=1`

Hosted architecture:

```text
Browser
  -> Vercel static Vite app
  -> /api/* rewrite
  -> Python FastAPI function
  -> Supabase
```

## Environment Variables

Core env yang relevan:

- Supabase URL/anon/service role.
- Provider API keys.
- 9router base URL/API key/model.
- `VOICEIDE_SECRET_KEY` untuk hosted provider secret encryption.
- Default workspace/local settings.

Frontend Supabase env dan backend Supabase env berbeda fungsi:

- Frontend anon key untuk client auth/readiness.
- Backend service role key untuk server-side persistence/RAG/jobs.

## Serverless Constraints

Hosted/serverless mode punya batas:

- Filesystem ephemeral.
- Long-running process tidak stabil seperti VM.
- Background worker harus bounded.
- Preview runner di serverless tidak selalu cocok untuk semua use case.
- Command execution harus lebih ketat.

Karena itu Appora punya local mode dan hosted mode branch.

## Cron Worker

Vercel cron memanggil:

```text
/api/agent/worker/run?limit=1
```

Worker auth:

- Hosted/serverless harus memakai worker secret.
- Local/dev bisa lebih longgar untuk test.

Worker harus menjaga profile context saat resume job.

## Verification Before Merge

Minimal sebelum menyatakan selesai:

```bash
python3 -m compileall -q api
npm run test:agent-regression
npm run lint
npm run build
git diff --check
```

Untuk perubahan UI/preview besar, jalankan Playwright/browser verification bila dev server tersedia.

## Cleanup Rules

Jangan commit:

- `dist/`
- `__pycache__/`
- `*.tsbuildinfo`
- `.tmp-*`
- local benchmark workspaces,
- secret-bearing `.env`.

## Operational Risks

- Vercel function duration bisa tidak cukup untuk heavy agent run.
- Preview runner process model lebih cocok local daripada serverless.
- Supabase readiness partial bisa membingungkan user bila warning kurang jelas.
- 9router/provider failures harus tidak membuat global cooldown semua provider.
- Build bundle cukup besar karena Supabase/motion/icons; manual chunks membantu tapi belum menyelesaikan semua.
